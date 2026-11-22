import hashlib
import json

import pytest

from chatcopilot.deployment.packaging import LINUX, SCHEMA, WINDOWS, read_plan, resolve_plan


def record(raw=b"", *, executable=False):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "executable": executable}


def test_discovers_ordinary_recipes_without_application_categories():
    static = {"integrations/notes/index.html": record()}
    plan = resolve_plan("notes", static)
    assert (plan.recipe, plan.platforms, plan.docker) == ("static", (WINDOWS, LINUX), False)

    clients = {
        "integrations/tool/install.json": record(),
        "integrations/tool/client/app": record(executable=True),
    }
    plan = resolve_plan("tool", clients, {"clients": {LINUX: "client/app"}}, default_recipe="native")
    assert (plan.recipe, plan.platforms, plan.clients) == ("client", (LINUX,), {LINUX: "client/app"})

    service = {
        "integrations/desk/install.json": record(),
        "integrations/desk/compose.yaml": record(),
    }
    plan = resolve_plan("desk", service, {"schema": SCHEMA, "requirements": ["Docker"]})
    assert (plan.recipe, plan.platforms, plan.docker, plan.requirements) == (
        "service", (WINDOWS, LINUX), True, ("Docker",)
    )


def test_reads_old_frozen_declaration_and_host_only_recipes(tmp_path):
    app = "desk"
    directory = tmp_path / "integrations" / app
    directory.mkdir(parents=True)
    raw = json.dumps({"entry": {"service": "web", "port": 8080}}).encode()
    (directory / "install.json").write_bytes(raw)
    (directory / "compose.yaml").write_text("services: {}")
    manifest = {
        f"integrations/{app}/install.json": record(raw),
        f"integrations/{app}/compose.yaml": record(b"services: {}"),
    }
    assert read_plan(tmp_path, manifest, app).recipe == "service"

    native = resolve_plan("reader", {"bots/reader/bot.yaml": record()}, default_recipe="native")
    assert (native.recipe, native.platforms) == ("native", (WINDOWS,))
    native_with_page = resolve_plan(
        "reader",
        {"bots/reader/bot.yaml": record(), "integrations/reader/index.html": record()},
        default_recipe="native",
    )
    assert native_with_page.recipe == "native"
    gateway = resolve_plan("robot", {"bots/robot/bot.yaml": record()}, default_recipe="gateway")
    assert (gateway.recipe, gateway.docker) == ("gateway", True)


def test_resolves_root_and_custom_application_directories():
    root = resolve_plan("root-app", {"index.html": record()}, application_dir=".")
    assert (root.recipe, root.prefix, root.descriptor) == ("static", "", None)
    custom = resolve_plan(
        "custom-app",
        {"products/portal/install.json": record(), "products/portal/compose.yaml": record()},
        {},
        application_dir="products/portal",
    )
    assert (custom.recipe, custom.prefix, custom.descriptor) == (
        "service", "products/portal/", "products/portal/install.json"
    )
    for invalid in ("/absolute", "../outside", "products/../outside", "products\\portal"):
        with pytest.raises(ValueError, match="规范相对路径"):
            resolve_plan("custom-app", (), application_dir=invalid)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"schema": "agentstrata-install/v9"}, "schema"),
        ({"recipe": "native"}, "static、client 或 service"),
        ({"recipe": "static", "platforms": [["windows-x64"]]}, "安装平台"),
        ({"recipe": "static", "platforms": {WINDOWS: True}}, "安装平台"),
        ({"recipe": "static", "platforms": [WINDOWS, WINDOWS]}, "安装平台"),
    ],
)
def test_rejects_unknown_recipe_schema_and_malformed_platforms(spec, message):
    members = {"integrations/app/install.json": record(), "integrations/app/index.html": record()}
    with pytest.raises(ValueError, match=message):
        resolve_plan("app", members, spec)


def test_rejects_recipe_that_conflicts_with_discovered_source():
    members = {
        "integrations/app/install.json": record(),
        "integrations/app/compose.yaml": record(),
        "integrations/app/index.html": record(),
    }
    with pytest.raises(ValueError, match="冲突"):
        resolve_plan("app", members, {"recipe": "static"})
    with pytest.raises(ValueError, match="覆盖"):
        resolve_plan("app", members, {"recipe": "service"}, default_recipe="gateway")

def test_host_companion_and_native_platform_cannot_be_redirected():
    from studio.installer import preview

    members = {"integrations/pet/install.json": record(), "integrations/pet/index.html": record()}
    with pytest.raises(ValueError, match="桌宠"):
        preview("companion", "pet", members, {"recipe": "static"})
    with pytest.raises(ValueError, match="Windows x64"):
        resolve_plan("pet", members, {"platforms": [LINUX]}, default_recipe="native")
