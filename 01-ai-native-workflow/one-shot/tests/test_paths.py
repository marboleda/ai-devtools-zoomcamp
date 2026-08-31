import unittest

from weekly_feedback import paths


class ClassificationTests(unittest.TestCase):
    def test_tests_are_detected(self):
        for path in [
            "tests/test_app.py",
            "test/helpers.py",
            "src/__tests__/app.tsx",
            "app/models_test.go",
            "web/Button.test.tsx",
            "web/Button.spec.ts",
            "java/src/UserServiceTest.java",
            "conftest.py",
        ]:
            with self.subTest(path=path):
                self.assertEqual(paths.kind(path), "test")

    def test_docs_are_detected(self):
        for path in ["README.md", "docs/guide.rst", "CHANGELOG.md", "documentation/a/b.md"]:
            with self.subTest(path=path):
                self.assertEqual(paths.kind(path), "docs")

    def test_source_is_detected(self):
        for path in ["src/app.py", "web/main.tsx", "cmd/server/main.go", "notebooks/week1.ipynb"]:
            with self.subTest(path=path):
                self.assertEqual(paths.kind(path), "source")

    def test_config_is_detected(self):
        for path in [
            "pyproject.toml",
            "package.json",
            "uv.lock",
            "requirements-dev.txt",
            ".github/workflows/ci.yml",
            "Dockerfile",
        ]:
            with self.subTest(path=path):
                self.assertEqual(paths.kind(path), "config")

    def test_unknown_extension_is_other(self):
        self.assertEqual(paths.kind("assets/logo.png"), "other")

    def test_test_beats_source_and_docs(self):
        self.assertTrue(paths.is_test("tests/test_app.py"))
        self.assertFalse(paths.is_source("tests/test_app.py"))
        self.assertFalse(paths.is_docs("tests/README.md"))

    def test_windows_separators_are_understood(self):
        self.assertEqual(paths.kind(r"tests\test_app.py"), "test")

    def test_directory_named_test_only_counts_as_a_parent(self):
        # A *file* called "tests" is not a test directory.
        self.assertEqual(paths.kind("src/spec/loader.py"), "test")
        self.assertEqual(paths.kind("src/specification.py"), "source")


if __name__ == "__main__":
    unittest.main()
