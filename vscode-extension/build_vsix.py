"""Build odoo-ci-<version>.vsix from this folder, without node or vsce.

A .vsix is a zip: [Content_Types].xml, extension.vsixmanifest, and the
extension's files under extension/. VS Code installs it with
    code --install-extension odoo-ci-<version>.vsix
"""
import json
import pathlib
import zipfile
from xml.sax.saxutils import escape

HERE = pathlib.Path(__file__).resolve().parent
FILES = [
    "package.json",
    "extension.js",
    "cockpit.js",
    "remote.js",
    "translations.js",
    "rulefixes.js",
    "detect.js",
    "inputs.js",
    "terminal_commands.js",
    "project.js",
    "graph.js",
    "media/cockpit.html",
    "media/translations.html",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "media/ci.svg",
    "media/studio.svg",
    "media/icon.png",
    # the pictures of README.md
    "media/screenshots/home.png",
    "media/screenshots/setup-notice.png",
    "media/screenshots/graph-map.png",
    "media/screenshots/server.png",
    "media/screenshots/databases.png",
    "media/screenshots/quality.png",
    "media/screenshots/errors.png",
    "media/screenshots/tools.png",
    "media/screenshots/remote.png",
    "media/screenshots/light-theme.png"
]

pkg = json.loads((HERE / "package.json").read_text(encoding="utf-8"))
out = HERE / f"{pkg['name']}-{pkg['version']}.vsix"

content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension=".json" ContentType="application/json"/>
<Default Extension=".js" ContentType="application/javascript"/>
<Default Extension=".svg" ContentType="image/svg+xml"/>
<Default Extension=".png" ContentType="image/png"/>
<Default Extension=".md" ContentType="text/markdown"/>
<Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""

manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{pkg['name']}" Version="{pkg['version']}" Publisher="{pkg['publisher']}" />
    <DisplayName>{escape(pkg['displayName'])}</DisplayName>
    <Description xml:space="preserve">{escape(pkg['description'])}</Description>
    <Tags>odoo,odoo19,odoo18,odoo17,odoo20,ci,testing,xpath,i18n,studio</Tags>
    <Categories>{','.join(pkg['categories'])}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Icon>extension/media/icon.png</Icon>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{pkg['engines']['vscode']}" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace" />
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value="" />
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="extension/media/icon.png" Addressable="true" />
  </Assets>
</PackageManifest>
"""

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("[Content_Types].xml", content_types)
    zf.writestr("extension.vsixmanifest", manifest)
    for rel in FILES:
        zf.write(HERE / rel, f"extension/{rel}")

print(f"Built package: {out}")
