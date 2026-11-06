import unittest

from profile_metadata import LABEL_PREFIX, resource_labels


class ProfileMetadata(unittest.TestCase):
    def labels(self, **geometry):
        return resource_labels({"plugin": {"managed_runtime": {"profiles": {"harbor-v1": geometry}}}})

    def test_floor_includes_upper_and_usable_root(self):
        self.assertEqual(self.labels(writable_mb=4096), {LABEL_PREFIX + "harbor-v1": "8192"})
        self.assertEqual(self.labels(writable_mb=4096, minimum_root_mb=8192), {LABEL_PREFIX + "harbor-v1": "12288"})

    def test_invalid_geometry_fails_before_advertisement(self):
        for geometry in ({}, {"writable_mb": 0}, {"writable_mb": True}, {"writable_mb": 4096, "minimum_root_mb": -1}, {"writable_mb": 1 << 44}):
            with self.subTest(geometry=geometry), self.assertRaises(ValueError):
                self.labels(**geometry)
