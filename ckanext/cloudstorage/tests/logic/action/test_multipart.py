# -*- coding: utf-8 -*-

from io import BytesIO

import requests
import pytest
import ckan.plugins.toolkit as toolkit
from ckan.tests import factories, helpers

from ckanext.cloudstorage.storage import ResourceCloudStorage
from ckanext.cloudstorage.utils import FakeFileStorage


def _patch_resource_patch_fails(monkeypatch, message="resource_patch failed"):
    _orig = toolkit.get_action

    def get_action(name):
        if name == "resource_patch":

            def _raise(*_a, **_kw):
                raise RuntimeError(message)

            return _raise
        return _orig(name)

    monkeypatch.setattr(
        "ckanext.cloudstorage.logic.action.multipart.toolkit.get_action",
        get_action,
    )


@pytest.mark.usefixtures(
    "with_driver_options", "with_plugins", "with_request_context", "clean_db"
)
class TestMultipartUpload(object):
    def test_upload(self):
        filename = "file.txt"
        res = factories.Resource()
        multipart = helpers.call_action(
            "cloudstorage_initiate_multipart",
            id=res["id"],
            name="file.txt",
            size=1024 * 1024 * 5 * 2,
        )
        storage = ResourceCloudStorage(res)
        assert (
            storage.path_from_filename(res["id"], filename)
            == multipart["name"]
        )
        assert storage.get_url_from_filename(res["id"], filename) is None

        fp = BytesIO(b"b" * 1024 * 1024 * 5)
        fp.seek(0)
        helpers.call_action(
            "cloudstorage_upload_multipart",
            uploadId=multipart["id"],
            partNumber=1,
            upload=FakeFileStorage(fp, filename),
        )

        assert storage.get_url_from_filename(res["id"], filename) is None

        fp = BytesIO(b"a" * 1024 * 1024 * 5)
        fp.seek(0)
        helpers.call_action(
            "cloudstorage_upload_multipart",
            uploadId=multipart["id"],
            partNumber=2,
            upload=FakeFileStorage(fp, filename),
        )

        assert storage.get_url_from_filename(res["id"], filename) is None

        result = helpers.call_action(
            "cloudstorage_finish_multipart", uploadId=multipart["id"]
        )
        assert result["commited"]
        assert result["pending_flag_cleared"] is True
        assert storage.get_url_from_filename(res["id"], filename)

    def test_finish_multipart_pending_flag_when_resource_patch_fails(
        self, monkeypatch
    ):
        filename = "file.txt"
        res = factories.Resource()
        multipart = helpers.call_action(
            "cloudstorage_initiate_multipart",
            id=res["id"],
            name="file.txt",
            size=1024 * 1024 * 5 * 2,
        )
        fp = BytesIO(b"b" * 1024 * 1024 * 5)
        fp.seek(0)
        helpers.call_action(
            "cloudstorage_upload_multipart",
            uploadId=multipart["id"],
            partNumber=1,
            upload=FakeFileStorage(fp, filename),
        )
        fp = BytesIO(b"a" * 1024 * 1024 * 5)
        fp.seek(0)
        helpers.call_action(
            "cloudstorage_upload_multipart",
            uploadId=multipart["id"],
            partNumber=2,
            upload=FakeFileStorage(fp, filename),
        )
        _patch_resource_patch_fails(monkeypatch)
        result = helpers.call_action(
            "cloudstorage_finish_multipart", uploadId=multipart["id"]
        )
        assert result["commited"] is True
        assert result["pending_flag_cleared"] is False

    def test_abort_multipart_pending_flag_when_resource_patch_fails(
        self, monkeypatch
    ):
        res = factories.Resource()
        _patch_resource_patch_fails(monkeypatch)
        result = helpers.call_action("cloudstorage_abort_multipart", id=res["id"])
        assert result["aborted"] == []
        assert result["pending_flag_cleared"] is False

    def test_upload_without_resource(self):
        res = {"id": "random-id"}
        filename = "file.txt"
        multipart = helpers.call_action(
            "cloudstorage_initiate_multipart",
            id=res["id"],
            name=filename,
            size=1024 * 1024 * 5 * 2,
        )
        storage = ResourceCloudStorage(res)
        assert (
            storage.path_from_filename(res["id"], filename)
            == multipart["name"]
        )
        assert storage.get_url_from_filename(res["id"], filename) is None

        fp = BytesIO(b"b" * 1024 * 1024 * 5)
        fp.seek(0)
        helpers.call_action(
            "cloudstorage_upload_multipart",
            uploadId=multipart["id"],
            partNumber=1,
            upload=FakeFileStorage(fp, filename),
        )

        assert storage.get_url_from_filename(res["id"], filename) is None

        fp = BytesIO(b"a" * 1024 * 1024 * 5)
        fp.seek(0)
        helpers.call_action(
            "cloudstorage_upload_multipart",
            uploadId=multipart["id"],
            partNumber=2,
            upload=FakeFileStorage(fp, filename),
        )

        assert storage.get_url_from_filename(res["id"], filename) is None

        result = helpers.call_action(
            "cloudstorage_finish_multipart", uploadId=multipart["id"]
        )
        assert result["commited"]
        assert result["pending_flag_cleared"] is True
        assert storage.get_url_from_filename(res["id"], filename)

    def test_reupload(self):
        filename = "file.txt"
        res = factories.Resource()
        from icecream import ic
        ic(res["id"])

        fp = BytesIO(b"b" * 10)
        fp.seek(0)
        res = _upload(res, filename, 10, [fp])

        storage = ResourceCloudStorage(res)
        url = storage.get_url_from_filename(res["id"], filename)
        assert url
        assert requests.get(url).content == fp.getvalue()

        fp = BytesIO(b"a" * 10)
        fp.seek(0)
        res = _upload(res, filename, 10, [fp])

        storage = ResourceCloudStorage(res)
        url = storage.get_url_from_filename(res["id"], filename)
        assert url
        assert requests.get(url).content == fp.getvalue()


def _upload(res, filename, size, parts):
    multipart = helpers.call_action(
        "cloudstorage_initiate_multipart",
        id=res["id"],
        name=filename,
        size=size,
    )

    for idx, part in enumerate(parts, 1):
        helpers.call_action(
            "cloudstorage_upload_multipart",
            uploadId=multipart["id"],
            partNumber=idx,
            upload=FakeFileStorage(part, filename),
        )

    result = helpers.call_action(
        "cloudstorage_finish_multipart", uploadId=multipart["id"]
    )
    assert result["commited"]
    assert result["pending_flag_cleared"] is True
    return helpers.call_action("resource_update", **dict(res, url_type="upload", url=filename))
