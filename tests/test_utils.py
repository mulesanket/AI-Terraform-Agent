# tests/test_utils.py
import os
import tempfile
import pytest

from utils import validate_path, safe_read_file, safe_write_file, run_cmd_capture


class TestValidatePath:
    def test_valid_path_inside_workdir(self, workdir):
        path = os.path.join(workdir, "main.tf")
        result = validate_path(path, workdir)
        assert result == os.path.realpath(path)

    def test_blocks_parent_traversal(self, workdir):
        with pytest.raises(PermissionError, match="Access denied"):
            validate_path(os.path.join(workdir, "..", "..", "etc", "passwd"), workdir)

    def test_blocks_absolute_outside_path(self, workdir):
        with pytest.raises(PermissionError, match="Access denied"):
            validate_path(tempfile.gettempdir(), workdir)

    def test_allows_workdir_itself(self, workdir):
        result = validate_path(workdir, workdir)
        assert result == os.path.realpath(workdir)

    def test_blocks_symlink_escape(self, workdir, tmp_path):
        """Symlinks that resolve outside workdir should be blocked."""
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("sensitive")
        link = os.path.join(workdir, "sneaky_link")
        try:
            os.symlink(str(secret), link)
        except OSError:
            pytest.skip("Symlink creation not supported")
        with pytest.raises(PermissionError, match="Access denied"):
            validate_path(link, workdir)


class TestSafeReadFile:
    def test_reads_file_within_workdir(self, workdir):
        content = safe_read_file(os.path.join(workdir, "main.tf"), base_dir=workdir)
        assert "null_resource" in content

    def test_blocks_read_outside_workdir(self, workdir):
        with pytest.raises(PermissionError):
            safe_read_file(os.path.join(workdir, "..", "..", "etc", "passwd"), base_dir=workdir)

    def test_reads_without_base_dir(self, workdir):
        # Without base_dir, no path validation occurs
        content = safe_read_file(os.path.join(workdir, "main.tf"))
        assert "null_resource" in content


class TestSafeWriteFile:
    def test_writes_file_within_workdir(self, workdir):
        path = os.path.join(workdir, "output.tf")
        safe_write_file(path, "# test", base_dir=workdir)
        assert os.path.exists(path)
        with open(path) as f:
            assert f.read() == "# test"

    def test_creates_nested_dirs(self, workdir):
        path = os.path.join(workdir, "sub", "dir", "file.tf")
        safe_write_file(path, "# nested", base_dir=workdir)
        assert os.path.exists(path)

    def test_blocks_write_outside_workdir(self, workdir):
        with pytest.raises(PermissionError):
            safe_write_file(
                os.path.join(workdir, "..", "evil.tf"),
                "# hacked",
                base_dir=workdir,
            )


class TestRunCmdCapture:
    def test_successful_command(self):
        rc, out, err = run_cmd_capture(["python", "-c", "print('hello')"])
        assert rc == 0
        assert "hello" in out

    def test_failed_command(self):
        rc, out, err = run_cmd_capture(["python", "-c", "raise SystemExit(1)"])
        assert rc == 1

    def test_nonexistent_command(self):
        rc, out, err = run_cmd_capture(["nonexistent_binary_xyz"])
        assert rc == 1
        assert err  # Should have error message

    def test_timeout(self):
        rc, out, err = run_cmd_capture(
            ["python", "-c", "import time; time.sleep(10)"], timeout=1
        )
        assert rc == 1
        assert "timed out" in err.lower()
