#!/usr/bin/env python3
"""
Saveit Multi-Platform & Multi-Target Build Tool
Builds standalone GUI and CLI executables for Windows and Linux with customizable branding.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import branding

PLATFORM = "windows" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
DEFAULT_NAME = branding.DEFAULT_APP_NAME
DEFAULT_TITLE = branding.DEFAULT_APP_TITLE
DEFAULT_SUBTITLE = branding.DEFAULT_APP_SUBTITLE
DEFAULT_VERSION = branding.DEFAULT_APP_VERSION
DEFAULT_AUTHOR = branding.DEFAULT_APP_AUTHOR
DEFAULT_DESC = branding.DEFAULT_APP_DESCRIPTION
DEFAULT_COPYRIGHT = branding.DEFAULT_APP_COPYRIGHT


def parse_version_tuple(version_str: str) -> Tuple[int, int, int, int]:
    """Parses a version string like '2.1.0' into a 4-integer tuple (2, 1, 0, 0)."""
    digits = [int(p) for p in re.findall(r"\d+", version_str)]
    while len(digits) < 4:
        digits.append(0)
    return tuple(digits[:4])


def generate_windows_version_info(
    output_path: Path,
    app_name: str,
    app_title: str,
    app_version: str,
    app_author: str,
    app_copyright: str,
):
    """Generates a PyInstaller-compatible Windows file version info resource file."""
    v_tuple = parse_version_tuple(app_version)
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v_tuple},
    prodvers={v_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [StringStruct('CompanyName', '{app_author}'),
           StringStruct('FileDescription', '{app_title}'),
           StringStruct('FileVersion', '{app_version}'),
           StringStruct('InternalName', '{app_name}'),
           StringStruct('LegalCopyright', '{app_copyright}'),
           StringStruct('OriginalFilename', '{app_name}.exe'),
           StringStruct('ProductName', '{app_name}'),
           StringStruct('ProductVersion', '{app_version}')]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    output_path.write_text(content, encoding="utf-8")


def ensure_default_icons(assets_dir: Path):
    """Generates default application icons if missing."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    ico_path = assets_dir / "icon.ico"
    png_path = assets_dir / "icon.png"

    if not ico_path.exists() or not png_path.exists():
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (256, 256), color=(0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([8, 8, 248, 248], fill="#2AABEE")
            draw.rounded_rectangle([72, 56, 184, 200], radius=16, fill="white")
            draw.rectangle([92, 56, 164, 110], fill="#2AABEE")
            draw.rounded_rectangle([100, 70, 156, 96], radius=4, fill="white")
            draw.rounded_rectangle([92, 140, 164, 184], radius=6, fill="#2AABEE")

            if not png_path.exists():
                img.save(png_path, format="PNG")
            if not ico_path.exists():
                img.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        except Exception as e:
            print(f"[Notice] Could not auto-generate default icon: {e}")


def interactive_branding_prompt() -> Dict[str, Any]:
    """Interactive wizard to configure app branding before building."""
    print("\n" + "=" * 60)
    print("      Saveit Executable Branding Configuration Wizard")
    print("=" * 60)

    def prompt(label: str, default: str) -> str:
        val = input(f"{label} [{default}]: ").strip()
        return val if val else default

    mode = prompt("Build Mode (gui, cli, both)", "both").lower()
    if mode not in {"gui", "cli", "both"}:
        mode = "both"

    name = prompt("Application Name (binary name)", DEFAULT_NAME)
    title = prompt("Display Title", f"{name} — Telegram Media Saver")
    subtitle = prompt("Subtitle (for GUI)", "Telegram Userbot")
    version = prompt("Version", DEFAULT_VERSION)
    author = prompt("Author / Organization", DEFAULT_AUTHOR)
    description = prompt("Short Description", DEFAULT_DESC)
    copyright_txt = prompt("Copyright Notice", DEFAULT_COPYRIGHT)
    icon = prompt("Icon Path (.ico for Windows / .png for Linux, or empty for default)", "")

    return {
        "mode": mode,
        "app_name": name,
        "app_title": title,
        "app_subtitle": subtitle,
        "app_version": version,
        "app_author": author,
        "app_description": description,
        "app_copyright": copyright_txt,
        "icon": icon if icon else (f"assets/icon.ico" if PLATFORM == "windows" else "assets/icon.png"),
    }


def build_target(
    target_type: str,  # 'gui' or 'cli'
    brand_cfg: Dict[str, Any],
    dist_dir: Path,
    build_dir: Path,
    onefile: bool = True,
    clean: bool = True,
    no_upx: bool = False,
) -> Path:
    """Builds a single target (GUI or CLI) using PyInstaller."""
    app_name = brand_cfg["app_name"]
    output_bin_name = app_name if target_type == "gui" else f"{app_name}-CLI"
    entry_script = "gui.py" if target_type == "gui" else "Saveit.py"

    print(f"\n>> Building {target_type.upper()} Target: '{output_bin_name}' from '{entry_script}'...")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        output_bin_name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir / f"work_{target_type}"),
        "--specpath",
        str(build_dir),
        "--noconfirm",
    ]

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    if clean:
        cmd.append("--clean")

    if no_upx:
        cmd.append("--noupx")

    # Console vs Windowed mode
    if target_type == "gui":
        cmd.append("--noconsole" if PLATFORM == "windows" else "--windowed")
    else:
        cmd.append("--console")

    # Icon
    icon_path = brand_cfg.get("icon")
    if icon_path and Path(icon_path).exists():
        cmd.extend(["--icon", str(icon_path)])

    # Windows Version Info Resource
    if PLATFORM == "windows":
        version_file = build_dir / f"version_info_{target_type}.txt"
        generate_windows_version_info(
            version_file,
            app_name=output_bin_name,
            app_title=brand_cfg["app_title"],
            app_version=brand_cfg["app_version"],
            app_author=brand_cfg["app_author"],
            app_copyright=brand_cfg["app_copyright"],
        )
        cmd.extend(["--version-file", str(version_file)])

    # Collect CustomTkinter assets for GUI
    if target_type == "gui":
        cmd.extend(["--collect-all", "customtkinter"])

    # Hidden imports
    hidden_imports = [
        "telethon",
        "telethon.tl",
        "telethon.tl.alltlobjects",
        "telethon.extensions",
        "telethon.crypto",
        "tracker",
        "engine",
        "branding",
        "sqlite3",
        "dotenv",
    ]
    if target_type == "gui":
        hidden_imports.extend(["customtkinter", "PIL", "darkdetect", "packaging"])

    for hi in hidden_imports:
        cmd.extend(["--hidden-import", hi])

    # Data files to embed in the executable
    sep = ";" if PLATFORM == "windows" else ":"

    # Embed branding.json
    branding_json = Path("branding.json").resolve()
    if branding_json.exists():
        cmd.extend(["--add-data", f"{branding_json}{sep}."])

    # Embed .env.example
    env_ex = Path(".env.example").resolve()
    if env_ex.exists():
        cmd.extend(["--add-data", f"{env_ex}{sep}."])

    # Embed assets folder if exists
    assets_dir = Path("assets").resolve()
    if assets_dir.exists():
        cmd.extend(["--add-data", f"{assets_dir}{sep}assets"])

    # Target entry point
    cmd.append(str(Path(entry_script).resolve()))

    # Run PyInstaller
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"PyInstaller build failed for {target_type} (exit code: {result.returncode})")

    # Locate generated output binary
    ext = ".exe" if PLATFORM == "windows" else ""
    expected_bin = dist_dir / f"{output_bin_name}{ext}"
    if not expected_bin.exists():
        # Check if PyInstaller created a folder instead of single file
        dir_bin = dist_dir / output_bin_name / f"{output_bin_name}{ext}"
        if dir_bin.exists():
            expected_bin = dir_bin

    return expected_bin


def main():
    parser = argparse.ArgumentParser(
        description="Saveit Cross-Platform Executable Builder with Custom Branding",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build default GUI and CLI for the current OS:
  python build.py

  # Build GUI only with custom branding:
  python build.py --mode gui --name "MySaver" --title "My Company Media Saver" --version "3.0.0"

  # Build using a branding.json file:
  python build.py --config custom_branding.json

  # Launch the interactive wizard:
  python build.py --interactive
        """,
    )

    parser.add_argument(
        "--mode",
        choices=["gui", "cli", "both"],
        default="both",
        help="Target application to build: 'gui' (desktop windowed app), 'cli' (terminal console bot), or 'both' (default: both)",
    )
    parser.add_argument(
        "-n",
        "--name",
        type=str,
        default=None,
        help=f"Application base name for executable files (default: {DEFAULT_NAME})",
    )
    parser.add_argument(
        "-t",
        "--title",
        type=str,
        default=None,
        help=f"Full display title for window headers and banners",
    )
    parser.add_argument(
        "--subtitle",
        type=str,
        default=None,
        help=f"GUI subtitle text (default: {DEFAULT_SUBTITLE})",
    )
    parser.add_argument(
        "-v",
        "--version",
        type=str,
        default=None,
        help=f"Semantic version string (default: {DEFAULT_VERSION})",
    )
    parser.add_argument(
        "-a",
        "--author",
        type=str,
        default=None,
        help=f"Author or organization name (default: {DEFAULT_AUTHOR})",
    )
    parser.add_argument(
        "-d",
        "--description",
        type=str,
        default=None,
        help=f"Brief description of the application",
    )
    parser.add_argument(
        "-c",
        "--copyright",
        type=str,
        default=None,
        help=f"Legal copyright statement (default: {DEFAULT_COPYRIGHT})",
    )
    parser.add_argument(
        "-i",
        "--icon",
        type=str,
        default=None,
        help="Path to custom icon file (.ico on Windows, .png on Linux)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to JSON file containing branding configuration overrides",
    )
    parser.add_argument(
        "--dist-dir",
        type=str,
        default="dist",
        help="Output directory for generated binaries (default: dist)",
    )
    parser.add_argument(
        "--build-dir",
        type=str,
        default="build",
        help="Temporary build directory (default: build)",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not clean build cache before compiling",
    )
    parser.add_argument(
        "--no-upx",
        action="store_true",
        help="Disable UPX compression",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch interactive configuration wizard to customize branding",
    )
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Generate an unpacked directory of dependencies instead of a single standalone executable file",
    )

    args = parser.parse_args()

    # Step 1: Resolve branding configuration
    brand_cfg = {
        "mode": args.mode,
        "app_name": DEFAULT_NAME,
        "app_title": DEFAULT_TITLE,
        "app_subtitle": DEFAULT_SUBTITLE,
        "app_version": DEFAULT_VERSION,
        "app_author": DEFAULT_AUTHOR,
        "app_description": DEFAULT_DESC,
        "app_copyright": DEFAULT_COPYRIGHT,
        "icon": "assets/icon.ico" if PLATFORM == "windows" else "assets/icon.png",
    }

    # Load from config file if provided
    if args.config:
        cfg_path = Path(args.config)
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                brand_cfg.update(loaded)
            print(f"[Config] Loaded branding from {args.config}")
        else:
            print(f"[Warning] Config file {args.config} not found. Using defaults.")

    # Override with CLI arguments if provided
    if args.name:
        brand_cfg["app_name"] = args.name
    if args.title:
        brand_cfg["app_title"] = args.title
    elif not args.config and args.name:
        brand_cfg["app_title"] = f"{args.name} — Telegram Media Saver"
    if args.subtitle:
        brand_cfg["app_subtitle"] = args.subtitle
    if args.version:
        brand_cfg["app_version"] = args.version
    if args.author:
        brand_cfg["app_author"] = args.author
    if args.description:
        brand_cfg["app_description"] = args.description
    if args.copyright:
        brand_cfg["app_copyright"] = args.copyright
    if args.icon:
        brand_cfg["icon"] = args.icon
    if args.mode:
        brand_cfg["mode"] = args.mode

    # Interactive wizard
    if args.interactive:
        brand_cfg = interactive_branding_prompt()

    # Step 2: Ensure default icon assets
    assets_dir = Path("assets")
    ensure_default_icons(assets_dir)

    # Validate icon
    icon_candidate = brand_cfg.get("icon")
    if not icon_candidate or not Path(icon_candidate).exists():
        default_candidate = "assets/icon.ico" if PLATFORM == "windows" else "assets/icon.png"
        brand_cfg["icon"] = default_candidate if Path(default_candidate).exists() else None

    # Step 3: Write out branding.json for embedding
    branding_json_path = Path("branding.json")
    with open(branding_json_path, "w", encoding="utf-8") as f:
        json.dump(brand_cfg, f, indent=4)

    dist_dir = Path(args.dist_dir).resolve()
    build_dir = Path(args.build_dir).resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    # Banner
    print("=" * 70)
    print(f"  Building {brand_cfg['app_name']} (v{brand_cfg['app_version']})")
    print(f"  Platform:    {PLATFORM.upper()}")
    print(f"  Target Mode: {brand_cfg['mode'].upper()}")
    print(f"  Title:       {brand_cfg['app_title']}")
    print(f"  Author:      {brand_cfg['app_author']}")
    print(f"  Output Dir:  {dist_dir}")
    print("=" * 70)

    # Step 4: Execute builds
    targets_to_build = []
    if brand_cfg["mode"] in ("gui", "both"):
        targets_to_build.append("gui")
    if brand_cfg["mode"] in ("cli", "both"):
        targets_to_build.append("cli")

    generated_binaries = []
    for tgt in targets_to_build:
        out_bin = build_target(
            target_type=tgt,
            brand_cfg=brand_cfg,
            dist_dir=dist_dir,
            build_dir=build_dir,
            onefile=not args.onedir,
            clean=not args.no_clean,
            no_upx=args.no_upx,
        )
        generated_binaries.append((tgt, out_bin))

    # Step 5: Summary
    print("\n" + "=" * 70)
    print("  BUILD SUMMARY — SUCCESSFUL")
    print("=" * 70)
    for tgt, bin_path in generated_binaries:
        if bin_path.exists():
            size_mb = bin_path.stat().st_size / (1024 * 1024)
            print(f"  • [{tgt.upper()}] {bin_path.name:<25} ({size_mb:.2f} MB)")
            print(f"    Location: {bin_path}")
        else:
            print(f"  • [{tgt.upper()}] Binary location: {bin_path}")

    print("\nHow to run:")
    for tgt, bin_path in generated_binaries:
        rel = bin_path.relative_to(Path.cwd()) if bin_path.is_relative_to(Path.cwd()) else bin_path
        if PLATFORM == "windows":
            print(f"  {tgt.upper()}: {rel}")
        else:
            print(f"  {tgt.upper()}: ./{rel}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
