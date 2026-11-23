from __future__ import annotations

import pytest

from app.shell import ShellError, resolve_cwd, shell_needs_approval
from app.tools import execute_approved, execute_tool


async def test_shell_policy_classifies_reads_and_writes():
    assert shell_needs_approval("ls -la") is False
    assert shell_needs_approval("git status") is False
    assert shell_needs_approval("git diff --stat") is False
    assert shell_needs_approval("curl -s https://example.com") is False
    assert shell_needs_approval("wget https://example.com/file") is False
    assert shell_needs_approval("pip install requests") is True
    assert shell_needs_approval("git push origin main") is False
    assert shell_needs_approval("rm -rf /workspace/repo") is False
    assert shell_needs_approval("curl -X POST https://example.com") is True
    assert shell_needs_approval("cat /workspace/README.md") is False
    assert shell_needs_approval("cat /tmp/README.md") is False
    assert shell_needs_approval("cat /dev/null") is False
    assert shell_needs_approval("cd ~ && python3 -c \"print(1)\"") is False
    assert shell_needs_approval("cd $HOME && ls") is False
    assert shell_needs_approval("cat ~/notes.md") is False
    assert shell_needs_approval("cat ~root/.ssh/id_rsa") is True
    assert shell_needs_approval("cat /data/muse.db") is True
    assert shell_needs_approval("cat ../muse.db") is True
    assert shell_needs_approval("curl file:///data/muse.db") is True
    assert shell_needs_approval("curl --request=POST https://example.com") is True
    assert shell_needs_approval("curl --data=x https://example.com") is True


async def run_approved(app, command: str, **extra):
    services = app.state.services
    result = await execute_tool(
        services,
        "run_shell",
        {"command": command, **extra},
        chat_id="shell-test",
    )
    assert result["status"] == "pending_approval"
    approval = services.approvals.get(result["approval_id"])
    return await execute_approved(services, approval)


async def test_shell_is_disabled_by_default(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": False, "write_enabled": True})
    result = await execute_tool(
        services, "run_shell", {"command": "printf hi"}, chat_id="shell-test"
    )
    assert "Shell access is disabled" in result["error"]
    assert services.approvals.pending() == []


def test_shell_spec_cannot_auto_approve(app):
    spec = app.state.services.tools["run_shell"]
    assert spec.side_effect is True
    assert spec.always_approve is False


async def test_shell_requires_approval_and_runs(app):
    services = app.state.services
    services.prefs.update(
        {
            "shell_enabled": True,
            "write_enabled": True,
            "auto_approve_writes": True,
        }
    )
    result = await execute_tool(
        services,
        "run_shell",
        {"command": "python3 -c \"print('hello')\" /data"},
        chat_id="shell-test",
    )
    assert result["status"] == "pending_approval"
    approval = services.approvals.get(result["approval_id"])
    executed = await execute_approved(services, approval)
    assert executed["ok"] is True
    assert executed["result"]["stdout"] == "hello\n"
    assert executed["result"]["exit_code"] == 0
    assert executed["result"]["stderr"] == ""


async def test_read_only_shell_command_runs_without_approval(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "write_enabled": True})
    result = await execute_tool(
        services, "run_shell", {"command": "pwd"}, chat_id="shell-read"
    )
    assert result["ok"] is True
    assert result["result"]["stdout"].strip() == str(
        services.settings.shell_workspace.resolve()
    )
    assert services.approvals.pending() == []


async def test_normal_workspace_command_runs_without_approval(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "write_enabled": True})
    result = await execute_tool(
        services,
        "run_shell",
        {"command": "python3 -c \"print('auto')\""},
        chat_id="shell-auto",
    )
    assert result["ok"] is True
    assert result["result"]["stdout"] == "auto\n"
    assert services.approvals.pending() == []


async def test_approved_shell_command_is_remembered(app, auth_client):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "write_enabled": True})
    command = "python3 -c \"print('approved')\" /data"
    first = await execute_tool(
        services, "run_shell", {"command": command}, chat_id="shell-cache"
    )
    assert first["status"] == "pending_approval"
    approved = auth_client.post(
        "/api/approvals/%d/approve" % first["approval_id"],
        json={"scope": "once"},
        headers={"X-Muse-Request": "1"},
    )
    assert approved.status_code == 200, approved.text
    assert command in (services.prefs.get("trusted_shell_commands") or [])

    second = await execute_tool(
        services, "run_shell", {"command": command}, chat_id="shell-cache"
    )
    assert second["ok"] is True
    assert second.get("status") is None
    assert services.approvals.pending() == []


async def test_approved_curl_trusts_all_curl_variations(app, auth_client):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "write_enabled": True})
    first_command = "curl --config /workspace/missing"
    first = await execute_tool(
        services, "run_shell", {"command": first_command}, chat_id="shell-curl-family"
    )
    assert first["status"] == "pending_approval"
    approved = auth_client.post(
        "/api/approvals/%d/approve" % first["approval_id"],
        json={"scope": "once"},
        headers={"X-Muse-Request": "1"},
    )
    assert approved.status_code == 200, approved.text
    assert "curl" in (services.prefs.get("trusted_shell_families") or [])

    variant = await execute_tool(
        services,
        "run_shell",
        {
            "command": "cd /tmp && curl --config missing.cfg"
        },
        chat_id="shell-curl-family",
    )
    assert variant.get("status") is None
    assert variant["ok"] is True
    assert services.approvals.pending() == []


async def test_install_command_requires_approval_then_remembered(app, auth_client):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "write_enabled": True})
    command = "python3 -m pip install --help"
    first = await execute_tool(
        services, "run_shell", {"command": command}, chat_id="shell-install"
    )
    assert first["status"] == "pending_approval"
    approved = auth_client.post(
        "/api/approvals/%d/approve" % first["approval_id"],
        json={"scope": "once"},
        headers={"X-Muse-Request": "1"},
    )
    assert approved.status_code == 200, approved.text
    assert command in (services.prefs.get("trusted_shell_commands") or [])

    again = await execute_tool(
        services, "run_shell", {"command": command}, chat_id="shell-install"
    )
    assert again["ok"] is True
    assert again.get("status") is None


async def test_shell_environment_is_sanitized(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "write_enabled": True})
    executed = await run_approved(
        app,
        "python3 -c \"import os; print(os.environ.get('OPENAI_API_KEY','')"
        "+os.environ.get('APP_PASSWORD','')+os.environ.get('SESSION_SECRET',''))\" /data",
    )
    assert executed["result"]["stdout"].strip() == ""


async def test_shell_cwd_cannot_escape_workspace(settings):
    with pytest.raises(ShellError):
        resolve_cwd(settings, "/etc")
    with pytest.raises(ShellError):
        resolve_cwd(settings, "../outside")
    assert resolve_cwd(settings, "") == settings.shell_workspace.resolve()
