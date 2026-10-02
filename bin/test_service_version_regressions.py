import unittest

from check_service_version_regressions import semver_precedence, version_regressions


class VersionRegressionTests(unittest.TestCase):
    def test_rejects_catalog_rollback(self):
        base = [{"id": "task-credential-broker", "version": "0.1.7"}]
        head = [{"id": "task-credential-broker", "version": "0.1.6"}]
        self.assertEqual(
            version_regressions(base, head),
            ["task-credential-broker: service version rollback 0.1.7 -> 0.1.6"],
        )

    def test_allows_upgrade_and_new_service(self):
        base = [{"id": "one", "version": "0.1.7"}]
        head = [
            {"id": "one", "version": "0.1.8"},
            {"id": "two", "version": "0.0.1"},
        ]
        self.assertEqual(version_regressions(base, head), [])

    def test_allows_first_semver_for_previously_unversioned_service(self):
        base = [{"id": "one", "version": None}]
        head = [{"id": "one", "version": "2.6.10"}]
        self.assertEqual(version_regressions(base, head), [])

    def test_compares_numeric_components_numerically(self):
        self.assertGreater(semver_precedence("1.10.0"), semver_precedence("1.9.99"))

    def test_stable_release_is_newer_than_prerelease(self):
        self.assertGreater(semver_precedence("1.0.0"), semver_precedence("1.0.0-rc.2"))
        self.assertLess(semver_precedence("1.0.0-rc.2"), semver_precedence("1.0.0-rc.10"))

    def test_build_metadata_does_not_change_precedence(self):
        self.assertEqual(semver_precedence("1.2.3+build.1"), semver_precedence("1.2.3+build.2"))

    def test_changed_non_semver_fails_closed(self):
        base = [{"id": "one", "version": "release-1"}]
        head = [{"id": "one", "version": "release-2"}]
        self.assertIn("cannot compare changed non-SemVer", version_regressions(base, head)[0])


if __name__ == "__main__":
    unittest.main()
