#!/usr/bin/env python
# -*- coding: utf-8 -*-
import datetime
import logging
import mimetypes

import ckan.model as model
import ckan.plugins.toolkit as toolkit
import libcloud.security
from ckan.lib.uploader import get_resource_uploader
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import NoResultFound
from werkzeug.datastructures import FileStorage as FlaskFileStorage

from ckanext.cloudstorage.model import MultipartPart, MultipartUpload
from ckanext.cloudstorage.storage import ResourceCloudStorage

config = toolkit.config

libcloud.security.VERIFY_SSL_CERT = True

log = logging.getLogger(__name__)


def _get_underlying_file(wrapper):
    if isinstance(wrapper, FlaskFileStorage):
        return wrapper.stream
    return wrapper.file


def _get_max_multipart_lifetime():
    value = float(config.get("ckanext.cloudstorage.max_multipart_lifetime", 7))
    return datetime.timedelta(value)


def _get_object_url(uploader, name):
    return "/" + uploader.container_name + "/" + name


def _clear_cloudstorage_multipart_pending(resource_id):
    """Remove ``cloudstorage_multipart_pending`` from resource extras and commit.

    Avoids ``resource_patch`` / ``package_update`` (full package round-trip and
    Solr via that path). Domain-object observers still run on commit so
    extensions (e.g. xloader) can react via ``IDomainObjectModification``.

    If the resource row is gone, returns without error (nothing to clear).
    """
    resource = model.Resource.get(resource_id)
    if resource is None:
        return

    extras = dict(resource.extras or {})
    if extras.pop("cloudstorage_multipart_pending", None) is None:
        return

    resource.extras = extras
    flag_modified(resource, "extras")
    model.Session.commit()


def _delete_multipart(upload, uploader):
    log.debug("_delete_multipart url {0}".format(_get_object_url(uploader, upload.name)))
    log.debug("_delete_multipart id {0}".format(upload.id))
    resp = uploader.driver.connection.request(
        _get_object_url(uploader, upload.name),
        params={
            "uploadId": upload.id
        },
        method="DELETE",
    )

    if not resp.success():
        raise toolkit.ValidationError(resp.error)

    upload.delete()
    upload.commit()
    return resp


def _save_part_info(n, etag, upload):
    try:
        part = (
            model.Session.query(MultipartPart)
            .filter(MultipartPart.n == n, MultipartPart.upload == upload)
            .one()
        )
    except NoResultFound:
        part = MultipartPart(n, etag, upload)
    else:
        part.etag = etag
    part.save()
    return part


def check_multipart(context, data_dict):
    """Check whether unfinished multipart upload already exists.

    :param context:
    :param data_dict: dict with required `id`
    :returns: None or dict with `upload` - existing multipart upload info
    :rtype: NoneType or dict

    """

    toolkit.check_access("cloudstorage_check_multipart", context, data_dict)
    id = toolkit.get_or_bust(data_dict, "id")
    try:
        upload = (
            model.Session.query(MultipartUpload)
            .filter_by(resource_id=id)
            .one()
        )
    except NoResultFound:
        return
    upload_dict = upload.as_dict()
    upload_dict["parts"] = (
        model.Session.query(MultipartPart)
        .filter(MultipartPart.upload == upload)
        .count()
    )
    return {"upload": upload_dict}


def initiate_multipart(context, data_dict):
    """Initiate new Multipart Upload.

    :param context:
    :param data_dict: dict with required keys:
        id: resource's id
        name: filename
        size: filesize

    :returns: MultipartUpload info
    :rtype: dict

    """

    toolkit.check_access("cloudstorage_initiate_multipart", context, data_dict)
    id, name, size = toolkit.get_or_bust(data_dict, ["id", "name", "size"])

    resource = model.Resource.get(id)
    if not resource:
        log.debug('Could not find resource %s', id)
        raise toolkit.ValidationError({
            "uploader": [
                "Resource was not found"
            ]
        })

    user_obj = model.User.get(context["user"])
    user_id = user_obj.id if user_obj else None

    uploader = get_resource_uploader({"multipart_name": name, "id": id})
    if not isinstance(uploader, ResourceCloudStorage):
        raise toolkit.ValidationError({
            "uploader": [
                "Must be ResourceCloudStorage or its subclass, not {}".format(
                    type(uploader)
                )
            ]
        })
    res_name = uploader.path_from_filename(id, name)

    log.info('initiate_multipart started for %s' % (res_name))
    upload_object = MultipartUpload.by_name(res_name)

    if upload_object is not None:
        _delete_multipart(upload_object, uploader)
        upload_object = None

    if upload_object is None:
        for old_upload in model.Session.query(MultipartUpload).filter_by(
            resource_id=id
        ):
            _delete_multipart(old_upload, uploader)

        # Find and remove previous file from this resourve
        _rindex = res_name.rfind("/")
        if ~_rindex:
            try:
                name_prefix = res_name[:_rindex]
                old_objects = uploader.driver.iterate_container_objects(
                    uploader.container, name_prefix
                )

                for obj in old_objects:
                    for similar in model.Session.query(
                        model.Resource
                    ).filter_by(url=obj.name[len(name_prefix) + 1:]):
                        if obj.name == uploader.path_from_filename(
                            similar.id, similar.url
                        ):
                            log.info(
                                "Leave cloud object because it is referenced"
                                " by resource %s: %s",
                                similar.id,
                                obj,
                            )
                            break
                    else:
                        log.info("Removing cloud object: %s" % obj)
                        obj.delete()
            except Exception as e:
                log.exception("[delete from cloud] %s" % e)

        headers = None
        content_type, _ = mimetypes.guess_type(res_name)
        if content_type:
            headers = {"Content-type": content_type}

        upload_object = MultipartUpload(
            uploader.driver._initiate_multipart(
                container=uploader.container,
                object_name=res_name,
                headers=headers,
            ),
            id,
            res_name,
            size,
            name,
            user_id,
        )

        upload_object.save()
    return upload_object.as_dict()


def upload_multipart(context, data_dict):
    toolkit.check_access("cloudstorage_upload_multipart", context, data_dict)
    upload_id, part_number, part_content = toolkit.get_or_bust(
        data_dict, ["uploadId", "partNumber", "upload"]
    )

    upload = model.Session.query(MultipartUpload).get(upload_id)
    uploader = get_resource_uploader({"id": upload.resource_id})

    data = _get_underlying_file(part_content).read()
    resp = uploader.driver.connection.request(
        _get_object_url(uploader, upload.name),
        params={"uploadId": upload_id, "partNumber": part_number},
        method="PUT",
        headers={"Content-Length": len(data)},
        data=data,
    )
    if resp.status != 200:
        log.error('upload_multipart failed for %s, part %s with status %s' % (upload.name, part_number, resp.status))
        raise toolkit.ValidationError("Upload failed: part %s" % part_number)

    _save_part_info(part_number, resp.headers["etag"], upload)
    log.info('upload_multipart uploaded %s, part %s' % (upload.name, part_number))
    return {"partNumber": part_number, "ETag": resp.headers["etag"]}


def finish_multipart(context, data_dict):
    """Called after all parts had been uploaded.

    Triggers call to `_commit_multipart` which will convert separate uploaded
    parts into single file

    :param context:
    :param data_dict: dict with required key `uploadId` - id of Multipart Upload that should be finished
    :returns: dict with ``commited`` (multipart committed to storage) and
        ``pending_flag_cleared`` (whether CKAN extra ``cloudstorage_multipart_pending`` was cleared)
    :rtype: dict

    """

    toolkit.check_access("cloudstorage_finish_multipart", context, data_dict)
    upload_id = toolkit.get_or_bust(data_dict, "uploadId")
    save_action = data_dict.get("save_action", False)
    upload = model.Session.query(MultipartUpload).get(upload_id)
    resource_id = upload.resource_id
    upload_name = upload.name

    chunks = [
        (part.n, part.etag)
        for part in model.Session.query(MultipartPart)
        .filter_by(upload_id=upload_id)
        .order_by(MultipartPart.n)
    ]
    uploader = get_resource_uploader({"id": resource_id})

    # Disable the block below, because it causes 404 error when user
    # uploads a file with the same name as previous file.
    # TODO: investigate possible side effects
    # try:
    #     obj = uploader.container.get_object(upload.name)
    #     obj.delete()
    # except Exception:
    #     pass

    uploader.driver._commit_multipart(
        container=uploader.container,
        object_name=upload_name,
        upload_id=upload_id,
        chunks=chunks,
    )

    upload.delete()
    upload.commit()

    # Clear the pending-upload flag without resource_patch/package_update.
    # Session.commit() still triggers IDomainObjectModification observers (xloader).
    pending_flag_cleared = True
    try:
        _clear_cloudstorage_multipart_pending(resource_id)
        log.debug(
            "cloudstorage multipart: cleared pending flag after finish "
            "(resource_id=%s upload_id=%s)",
            resource_id,
            upload_id,
        )
    except Exception:
        pending_flag_cleared = False
        log.exception(
            "cloudstorage multipart: failed to clear pending flag after "
            "finish (resource_id=%s upload_id=%s)",
            resource_id,
            upload_id,
        )

    if save_action and save_action == "go-metadata":
        try:
            res_dict = toolkit.get_action("resource_show")(
                context.copy(), {"id": data_dict.get("id")}
            )
            pkg_dict = toolkit.get_action("package_show")(
                context.copy(), {"id": res_dict["package_id"]}
            )
            if pkg_dict["state"] == "draft":
                toolkit.get_action("package_patch")(
                    dict(context.copy(), allow_state_change=True),
                    dict(id=pkg_dict["id"], state="active"),
                )
            resource = model.Session.query(model.Resource).get(res_dict["id"])
            resource.last_modified = datetime.datetime.utcnow()
            resource.commit()
        except Exception as e:
            log.error('finish_multipart failed for %s with error %s' % (upload_name, str(e)))
    log.info('finish_multipart successfully finished for %s' % (upload_name))
    return {"commited": True, "pending_flag_cleared": pending_flag_cleared}


def abort_multipart(context, data_dict):
    toolkit.check_access("cloudstorage_abort_multipart", context, data_dict)
    id = toolkit.get_or_bust(data_dict, ["id"])

    uploader = get_resource_uploader({"id": id})
    resource_uploads = MultipartUpload.resource_uploads(id)

    aborted = []
    for upload in resource_uploads:
        _delete_multipart(upload, uploader)

        aborted.append(upload.id)

    model.Session.commit()

    # Clear the pending-upload flag so the resource is not stranded with a
    # never-cleared flag after the user cancels a multipart upload.
    pending_flag_cleared = True
    try:
        _clear_cloudstorage_multipart_pending(id)
        log.debug(
            "cloudstorage multipart: aborted upload ids for resource %s: %s",
            id,
            aborted,
        )
    except Exception:
        pending_flag_cleared = False
        log.exception(
            "cloudstorage multipart: failed to clear pending flag after "
            "abort (resource_id=%s)",
            id,
        )

    return {"aborted": aborted, "pending_flag_cleared": pending_flag_cleared}


def clean_multipart(context, data_dict):
    """Clean old multipart uploads.

    :param context:
    :param data_dict:
    :returns: dict with:
        removed - amount of removed uploads.
        total - total amount of expired uploads.
        errors - list of errors raised during deletion. Appears when
        `total` and `removed` are different.
    :rtype: dict

    """

    toolkit.check_access("cloudstorage_clean_multipart", context, data_dict)
    delta = _get_max_multipart_lifetime()
    oldest_allowed = datetime.datetime.utcnow() - delta

    uploads_to_remove = model.Session.query(MultipartUpload).filter(
        MultipartUpload.initiated < oldest_allowed
    )

    result = {"removed": 0, "total": uploads_to_remove.count(), "errors": []}

    for upload in uploads_to_remove:
        uploader = get_resource_uploader({"id": upload.resource_id})

        try:
            _delete_multipart(upload, uploader)
        except toolkit.ValidationError as e:
            result["errors"].append(e.error_summary)
        else:
            result["removed"] += 1

    return result
