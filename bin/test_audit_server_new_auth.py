import unittest

from audit_server_new_auth import classify, repo_name


class ClassifyTest(unittest.TestCase):
    def test_server_run_is_ok(self):
        self.assertEqual(classify('server.Run("x", v, Handler)'), "ok")

    def test_new_with_keystore_auth_is_ok(self):
        self.assertEqual(classify("srv := server.New(cfg, server.WithKeystoreAuth())"), "ok")
        self.assertEqual(classify("srv := server.New(cfg, server.WithKeystoreAuthTier(t, true))"), "ok")

    def test_new_without_auth_is_flagged(self):
        self.assertEqual(classify("srv := server.New(cfg, server.WithMCP())"), "no-auth-option")

    def test_unrelated_file(self):
        self.assertEqual(classify("package main\nfunc main() {}"), "not-server-new")

    def test_repo_name(self):
        self.assertEqual(repo_name({"repo_url": "https://github.com/o/go_x.git"}), "go_x")
        self.assertEqual(repo_name({}), "")


if __name__ == "__main__":
    unittest.main()
