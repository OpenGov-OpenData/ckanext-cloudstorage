# -*- coding: utf-8 -*-
from urllib.parse import urlparse

import pytest
from ckan.tests import factories

from ckanext.cloudstorage import storage as cloudstorage_storage
from ckanext.cloudstorage.storage import (
    CloudStorage,
    ResourceCloudStorage,
    attachment_content_disposition,
)


@pytest.mark.usefixtures("with_driver_options", "with_plugins")
class TestCloudStorage(object):
    def test_props(self):
        storage = CloudStorage()
        assert storage.driver_options
        assert storage.driver_name
        assert storage.container_name
        assert storage.container
        assert not storage.leave_files
        assert not storage.use_secure_urls
        assert not storage.guess_mimetype


@pytest.mark.usefixtures("with_driver_options", "with_plugins")
class TestResourceCloudStorage(object):
    @pytest.mark.ckan_config("ckanext.cloudstorage.use_secure_urls", True)
    def test_secure_url_from_filename(self, create_with_upload):
        filename = "file.txt"
        resource = create_with_upload(
            "test", filename, package_id=factories.Dataset()["id"]
        )
        storage = ResourceCloudStorage(resource)
        if not storage.can_use_advanced_aws or not storage.use_secure_urls:
            pytest.skip("SecureURL not supported")
        url = storage.get_url_from_filename(resource["id"], filename)
        assert urlparse(url).query

    @pytest.mark.ckan_config("ckanext.cloudstorage.use_secure_urls", True)
    def test_hash_check(self, create_with_upload):
        filename = "file.txt"
        resource = create_with_upload(
            "test", filename, package_id=factories.Dataset()["id"]
        )
        storage = ResourceCloudStorage(resource)
        if not storage.can_use_advanced_aws or not storage.use_secure_urls:
            pytest.skip("SecureURL not supported")
        url = storage.get_url_from_filename(resource["id"], filename)
        resource = create_with_upload(
            "test", filename, action="resource_update", id=resource["id"]
        )

        assert urlparse(url).query

    @pytest.mark.ckan_config("ckanext.cloudstorage.parent_dir_name", "test-parent-dir")
    def test_file_path(self):
        filename = "file.txt"
        rid = "abcd1234-abcd-1234-abcd-1234abcd1234"
        file_path = ResourceCloudStorage.path_from_filename(rid, filename)

        assert file_path == "/test-parent-dir/resources/abcd1234-abcd-1234-abcd-1234abcd1234/file.txt"


class TestMultipartPendingFlag(object):
    """Verify the multipart branch in ResourceCloudStorage.__init__ sets
    the cloudstorage_multipart_pending flag so xloader skips submission
    until cloudstorage_finish_multipart clears it."""

    def _patch_cloud_storage(self, monkeypatch, advanced_aws=True):
        """Bypass real driver wiring so we can construct a
        ResourceCloudStorage without cloud credentials."""
        monkeypatch.setattr(
            cloudstorage_storage.CloudStorage, "__init__",
            lambda self: None,
        )
        monkeypatch.setattr(
            cloudstorage_storage.ResourceCloudStorage,
            "can_use_advanced_aws",
            property(lambda self: advanced_aws),
        )

    def test_multipart_name_sets_pending_flag(self, monkeypatch):
        self._patch_cloud_storage(monkeypatch, advanced_aws=True)

        resource = {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "multipart_name": "big-file.csv",
        }

        ResourceCloudStorage(resource)

        assert resource.get("cloudstorage_multipart_pending") == "True"
        assert resource.get("url_type") == "upload"

    def test_no_multipart_name_leaves_flag_unset(self, monkeypatch):
        """Plain resource_create / resource_update (non-multipart) must
        not set the pending flag, otherwise normal uploads would be
        blocked from xloader."""
        self._patch_cloud_storage(monkeypatch, advanced_aws=True)

        resource = {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "url": "http://example.com/file.csv",
        }

        ResourceCloudStorage(resource)

        assert "cloudstorage_multipart_pending" not in resource

    def test_multipart_name_sets_pending_flag_without_advanced_aws(
        self, monkeypatch
    ):
        """The pending flag must be raised independently of
        can_use_advanced_aws. The live part-upload APIs (initiate/upload/
        finish_multipart) still run on deployments where
        can_use_advanced_aws is False (e.g. S3 driver without 'host' in
        driver_options), so the xloader race exists there too and the flag
        must be set."""
        self._patch_cloud_storage(monkeypatch, advanced_aws=False)

        resource = {
            "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "multipart_name": "big-file.csv",
        }

        ResourceCloudStorage(resource)

        assert resource.get("cloudstorage_multipart_pending") == "True"


class TestAttachmentContentDisposition(object):
    def test_geojson_filename_is_an_attachment(self):
        header = attachment_content_disposition(
            "wicked_free_wifi_locations.geojson"
        )

        assert header.startswith("attachment;")
        assert 'filename="wicked_free_wifi_locations.geojson"' in header

    def test_header_breaks_are_removed(self):
        header = attachment_content_disposition('notes.json"\r\nX-Ignore: 1')

        assert "\r" not in header
        assert "\n" not in header
        assert header.startswith('attachment; filename="notes.json')


class TestSignedDownloadHeaders(object):
    def test_aws_url_includes_attachment_header(self, monkeypatch):
        import sys
        import types

        captured = {}

        class FakeConnection(object):
            def __init__(self, key, secret, host=None):
                self.auth_region_name = None

            def generate_url_sigv4(self, **params):
                captured.update(params)
                return "https://bucket.s3.amazonaws.com/file.geojson?signed=1"

        connection_module = types.ModuleType("boto.s3.connection")
        connection_module.S3Connection = FakeConnection
        monkeypatch.setitem(sys.modules, "boto", types.ModuleType("boto"))
        monkeypatch.setitem(sys.modules, "boto.s3", types.ModuleType("boto.s3"))
        monkeypatch.setitem(
            sys.modules, "boto.s3.connection", connection_module
        )
        monkeypatch.setattr(
            ResourceCloudStorage,
            "can_use_advanced_azure",
            property(lambda self: False),
        )
        monkeypatch.setattr(
            ResourceCloudStorage,
            "can_use_advanced_aws",
            property(lambda self: True),
        )
        monkeypatch.setattr(
            ResourceCloudStorage,
            "use_secure_urls",
            property(lambda self: True),
        )
        monkeypatch.setattr(
            ResourceCloudStorage,
            "driver_options",
            property(lambda self: {
                "key": "key",
                "secret": "secret",
                "host": "s3.amazonaws.com",
            }),
        )
        monkeypatch.setattr(
            ResourceCloudStorage,
            "container_name",
            property(lambda self: "bucket"),
        )

        storage = ResourceCloudStorage.__new__(ResourceCloudStorage)
        header = attachment_content_disposition(
            "wicked_free_wifi_locations.geojson"
        )
        url = storage.get_url_by_path(
            "resources/id/wicked_free_wifi_locations.geojson",
            content_type="application/geo+json",
            content_disposition=header,
        )

        assert url.startswith("https://bucket.s3.amazonaws.com/")
        assert (
            captured["response_headers"]["response-content-disposition"]
            == header
        )
        assert (
            captured["response_headers"]["Content-Type"]
            == "application/geo+json"
        )
