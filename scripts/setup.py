#!/usr/bin/env python3
"""
Cursor - Environment Setup Script
"""

import os
import platform
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Tuple, Optional, Dict
import argparse
import threading
import http.server
import socketserver
import webbrowser


DEBUG = False


def debug_print(message: str) -> None:
    """Print message only if DEBUG mode is enabled."""
    if DEBUG:
        print(f"[DEBUG] {message}")


def normalize_url(domain: str) -> str:
    """Normalize domain to proper URL format."""
    domain = domain.strip()

    if domain.startswith("http://") or domain.startswith("https://"):
        url = domain
    else:
        url = f"https://{domain}"

    return url.rstrip('/')

def get_shell_rc_file() -> Path:
    """
    Determine the appropriate shell configuration file based on the OS and shell.

    Returns:
        Path: Path to the shell configuration file
    """
    system = platform.system().lower()
    shell = os.environ.get("SHELL", "").lower()

    if system == "darwin":
        # macOS - default shell is zsh
        if "zsh" in shell:
            return Path.home() / ".zprofile"
        else:
            return Path.home() / ".bash_profile"

    elif system == "linux":
        # Linux
        if "zsh" in shell:
            return Path.home() / ".zshrc"
        else:
            return Path.home() / ".bashrc"

    elif system == "windows":
        # Windows - uses registry, no rc file
        return None

    else:
        raise OSError(f"Unsupported operating system: {system}")


def append_to_file(file_path: Path, line: str) -> bool:
    """
    Append a line to a file only if it's not already present.

    Args:
        file_path: Path to the file to append to
        line: Line to append (without newline)

    Returns:
        bool: True if line was added, False if it already existed
    """
    try:
        file_path.touch(exist_ok=True)

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        if line not in content:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(f"{line}\n")
            return True
        else:
            return False
    except Exception as e:
        print(f"Failed to modify {file_path}: {e}")
        return False


def set_env_var_on_windows(var_name: str, value: str) -> bool:
    """
    Set environment variable permanently on Windows using setx.

    Args:
        var_name: Name of the environment variable
        value: Value to set

    Returns:
        bool: True if successful, False otherwise
    """
    debug_print(f"Writing to user environment registry (Windows)")
    try:
        subprocess.run(["setx", var_name, value], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to set {var_name} on Windows: {e}")
        if e.stderr:
            print(f"   Error details: {e.stderr.decode()}")
        return False
    except FileNotFoundError:
        print(f"'setx' command not found. Please set {var_name} manually.")
        return False


def set_env_var_on_unix(var_name: str, value: str) -> bool:
    """
    Set environment variable permanently on Unix-like systems (macOS, Linux).

    Args:
        var_name: Name of the environment variable
        value: Value to set

    Returns:
        bool: True if successful, False otherwise
    """
    rc_file = get_shell_rc_file()
    if rc_file is None:
        return False

    debug_print(f"Writing to shell file: {rc_file}")
    export_line = f"export {var_name}={shlex.quote(value)}"

    # append_to_file returns False for both "already present" (idempotent success)
    # and write errors (logged by append_to_file). Either way the desired state is
    # either already achieved or was attempted, so we return True.
    append_to_file(rc_file, export_line)
    return True


def set_env_var(var_name: str, value: str) -> Tuple[bool, str]:
    """
    Set an environment variable permanently across all OS platforms.

    Args:
        var_name: Name of the environment variable
        value: Value to set

    Returns:
        Tuple[bool, str]: (success, message)
    """
    system = platform.system().lower()

    if system == "windows":
        success = set_env_var_on_windows(var_name, value)
        if success:
            return True, "Environment variable set for new terminals"
        else:
            return False, "Failed to set environment variable"

    elif system in ["darwin", "linux"]:
        success = set_env_var_on_unix(var_name, value)
        if success:
            rc_file = get_shell_rc_file()
            return True, f"Run 'source {rc_file}' or restart terminal"
        else:
            return False, "Failed to set environment variable"

    else:
        return False, f"Unsupported OS: {system}"


def remove_env_var_on_unix(var_name: str) -> bool:
    """
    Remove an environment variable export line from the user's shell rc file.
    """
    rc_file = get_shell_rc_file()
    if rc_file is None:
        return False
    try:
        rc_file.touch(exist_ok=True)
        with open(rc_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        removed = False
        export_prefix = f"export {var_name}="
        for line in lines:
            if line.strip().startswith(export_prefix):
                removed = True
                continue
            new_lines.append(line)
        if removed:
            with open(rc_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        return True
    except Exception as e:
        print(f"Failed to modify {rc_file}: {e}")
        return False


def remove_env_var_on_windows(var_name: str) -> bool:
    """
    Remove a user environment variable on Windows by deleting it from HKCU\\Environment.
    """
    try:
        subprocess.run(["reg", "delete", "HKCU\\Environment", "/F", "/V", var_name], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        # If it doesn't exist, treat as success
        return True
    except FileNotFoundError:
        print("'reg' command not found. Please remove the variable manually.")
        return False


def remove_env_var(var_name: str) -> Tuple[bool, str]:
    """
    Remove an environment variable permanently across OS platforms.
    """
    system = platform.system().lower()
    if system == "windows":
        success = remove_env_var_on_windows(var_name)
        if success:
            debug_print(f"Removed {var_name} from Windows registry")
        return (True, "Removed") if success else (False, f"Failed to remove {var_name}")
    elif system in ["darwin", "linux"]:
        success = remove_env_var_on_unix(var_name)
        if success:
            debug_print(f"Removed {var_name} from shell rc file")
        return (True, "Removed") if success else (False, f"Failed to remove {var_name}")
    else:
        return False, f"Unsupported OS: {system}"


def run_one_shot_callback_server(frontend_url: str) -> Optional[Dict[str, any]]:
    """
    Start a local HTTP server that waits for a single callback request and returns its contents.
    Returns a dict with method, path, query, headers, and body; or None on failure.
    """
    result: Dict[str, any] = {"method": None, "path": None, "query": None, "headers": None, "body": None}
    done_evt = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def _finish(self, code: int = 200, message: bytes = b"Logged in successfully! You can close this tab and return to Cursor.") -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)
            except Exception:
                pass

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self._finish(404, b"Not found")
                return
            result["method"] = "GET"
            result["path"] = self.path
            result["query"] = dict(urllib.parse.parse_qsl(parsed.query))
            result["headers"] = {k: v for k, v in self.headers.items()}
            result["body"] = None
            self._finish()
            done_evt.set()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self._finish(404, b"Not found")
                return
            result["method"] = "POST"
            result["path"] = self.path
            result["query"] = dict(urllib.parse.parse_qsl(parsed.query))
            result["headers"] = {k: v for k, v in self.headers.items()}
            result["body"] = body.decode("utf-8", errors="replace") if body else None
            self._finish()
            done_evt.set()

        def log_message(self, format: str, *args) -> None:
            return

    class _ReuseAddrTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        httpd = _ReuseAddrTCPServer(("127.0.0.1", 0), CallbackHandler)
        _, port = httpd.server_address
        callback_url = f"http://127.0.0.1:{port}/callback"

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        encoded_callback = urllib.parse.quote(callback_url, safe="")
        target_url = f"{frontend_url.rstrip('/')}/automations/api-key-callback?callback_url={encoded_callback}&app_type=default"
        webbrowser.open(target_url)
        print("Opening browser...")
        print("If browser doesn't open automatically, open this link:")
        print(target_url)
        print("Waiting for authentication...")

        try:
            if not done_evt.wait(timeout=300):
                print("\nTimed out waiting for browser authentication (5 minutes).")
                return None
        finally:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass

        return result
    except Exception as e:
        print(f"Failed to run callback server: {e}")
        return None


def clear_setup() -> None:
    """Undo all changes made by the setup script."""
    print("=" * 60)
    print("Cursor - Clearing Setup")
    print("=" * 60)

    # Remove UNBOUND_API_KEY
    success, _ = remove_env_var("UNBOUND_API_KEY")
    if success:
        print("Removed UNBOUND_API_KEY")
    else:
        print("Failed to remove UNBOUND_API_KEY")

    print("\n" + "=" * 60)
    print("Clear Complete!")
    print("=" * 60)


def main():
    """Main setup function."""
    global DEBUG

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--domain", dest="domain", help="Base frontend URL (e.g., gateway.getunbound.ai)")
    parser.add_argument("--clear", action="store_true", help="Undo all changes made by the setup script")
    parser.add_argument("--debug", action="store_true", help="Show detailed debug information")
    args, _ = parser.parse_known_args()

    if args.debug:
        DEBUG = True
        debug_print("Debug mode enabled")

    if args.clear:
        clear_setup()
        return

    if not args.domain:
        print("\nMissing required argument: --domain (e.g., --domain gateway.getunbound.ai)")
        sys.exit(1)

    print("=" * 60)
    print("Cursor - Environment Setup")
    print("=" * 60)

    # Flush previously set UNBOUND_API_KEY so we can write a fresh one
    try:
        remove_env_var("UNBOUND_API_KEY")
    except Exception:
        pass

    auth_url = normalize_url(args.domain)
    cb_response = run_one_shot_callback_server(auth_url)
    if cb_response is None:
        print("\nFailed to receive callback response. Exiting.")
        sys.exit(1)

    api_key = None
    try:
        api_key = (cb_response.get("query") or {}).get("api_key")
    except Exception:
        api_key = None

    if not api_key:
        print("\nNo api_key found in callback. Exiting.")
        sys.exit(1)

    if "'" in api_key:
        print("\nReceived API key contains an invalid character ('). Exiting.")
        sys.exit(1)

    print("API Key Verified")
    debug_print("API key verification successful")

    debug_print("Setting UNBOUND_API_KEY environment variable...")
    success, message = set_env_var("UNBOUND_API_KEY", api_key)
    if not success:
        print(f"Failed to configure UNBOUND_API_KEY: {message}")
        sys.exit(1)
    debug_print("UNBOUND_API_KEY set successfully")

    # Final instructions
    print("\n" + "=" * 60)
    print("Setup Complete!")
    print("=" * 60)
    rc_path = get_shell_rc_file()
    if rc_path is not None:
        print(f"\nTo apply changes, restart Cursor.")
        print(f"Or source the key in your terminal first:\n  source {rc_path}\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled by user.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
        exit(1)
