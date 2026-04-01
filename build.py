#!/usr/bin/env python3
"""Build Cartisse fonts from FontForge SFD sources.

Features:
- Adjustable embolden (`--embolden`)
- Optional T-series batch builds (`--t-series 8 20` => "Cartisse T8".."Cartisse T20")
- Kobo-friendly post-processing (style flags, autohint, kobofix KF variants)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
from dataclasses import dataclass

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_SRC_DIR = ROOT_DIR / "src"
DEFAULT_OUT_DIR = ROOT_DIR / "out"
DEFAULT_VERSION_FILE = ROOT_DIR / "VERSION"
DEFAULT_FAMILY = "Cartisse"
DEFAULT_EMBOLDEN = 16.0

KOBOFIX_URL = (
    "https://raw.githubusercontent.com/nicoverbruggen/kobo-font-fix/main/kobofix.py"
)

STYLE_MAP = {
    "Regular": ("Regular", "Book", 400),
    "Bold": ("Bold", "Bold", 700),
    "Italic": ("Italic", "Book", 400),
    "BoldItalic": ("Bold Italic", "Bold", 700),
}

# Optional explicit pair fixups for renderers with weak ligature support.
KERN_PAIRS: list[tuple[str, str, int]] = []

AUTOHINT_OPTS = [
    "--stem-width-mode=nss",
]


class BuildError(RuntimeError):
    """Raised when the font build fails."""


@dataclass(frozen=True)
class BuildTarget:
    family: str
    embolden: float


def load_font_version(version_file: Path) -> str:
    """Read version string from VERSION file."""
    if not version_file.is_file():
        raise BuildError(f"Missing VERSION file: {version_file}")

    value = version_file.read_text(encoding="utf-8").strip()
    if not value:
        raise BuildError(f"VERSION file is empty: {version_file}")
    return value


def parse_sfnt_revision(version: str) -> float | None:
    """Best-effort parse for head.fontRevision-compatible float value."""
    token = version.strip().split()[0]
    try:
        return float(token)
    except (ValueError, IndexError):
        return None


def find_fontforge(explicit_path: str | None = None) -> list[str]:
    """Return the FontForge command to invoke."""
    if explicit_path:
        ff_path = Path(explicit_path).expanduser()
        if ff_path.is_file() and os.access(ff_path, os.X_OK):
            return [str(ff_path)]
        raise BuildError(f"FontForge binary not executable: {ff_path}")

    on_path = shutil.which("fontforge")
    if on_path:
        return [on_path]

    mac_candidates = [
        "/Applications/FontForge.app/Contents/MacOS/FontForge",
        "/Applications/FontForge.app/Contents/Resources/opt/local/bin/fontforge",
    ]
    for candidate in mac_candidates:
        if Path(candidate).is_file():
            return [candidate]

    raise BuildError(
        "FontForge not found. Install it or pass --fontforge /path/to/fontforge"
    )


def run_fontforge_script(
    fontforge_cmd: list[str],
    script_text: str,
    verbose_fontforge: bool,
) -> None:
    """Execute a FontForge Python script passed on stdin."""
    cmd = fontforge_cmd + ["-lang=py", "-script", "-"]
    result = subprocess.run(
        cmd,
        input=script_text,
        capture_output=True,
        text=True,
    )

    if result.stdout:
        print(result.stdout, end="")

    if result.stderr:
        suppressed_overlap = 0
        suppressed_kern_note = 0
        for line in result.stderr.splitlines():
            if line.startswith("Copyright") or line.startswith(" License"):
                continue
            if line.startswith(" Version") or line.startswith(" Based on"):
                continue
            if line.startswith(" with many parts"):
                continue
            if "pkg_resources" in line:
                continue
            if "plugin_config.ini" in line:
                continue
            if not verbose_fontforge and "Internal Error (overlap)" in line:
                suppressed_overlap += 1
                continue
            if not verbose_fontforge and "ends at (-999999,-999999)" in line:
                suppressed_overlap += 1
                continue
            if not verbose_fontforge and line.startswith(
                "Note: On Windows many apps can have problems with this font's kerning"
            ):
                suppressed_kern_note += 1
                continue
            print(f"  [fontforge] {line}", file=sys.stderr)

        if suppressed_overlap:
            print(
                f"  [fontforge] Suppressed {suppressed_overlap} overlap warnings "
                "(use --verbose-fontforge to show all)",
                file=sys.stderr,
            )
        if suppressed_kern_note:
            print(
                f"  [fontforge] Suppressed {suppressed_kern_note} repeated old-kern notes",
                file=sys.stderr,
            )

    if result.returncode != 0:
        raise BuildError(f"FontForge failed with exit code {result.returncode}")


def collect_sources(src_dir: Path, only_fonts: set[str] | None) -> list[Path]:
    """Collect source .sfd files from the source directory."""
    sfd_files = sorted(src_dir.glob("*.sfd"))
    if only_fonts:
        sfd_files = [
            p for p in sfd_files if p.stem in only_fonts or p.name in only_fonts
        ]
    if not sfd_files:
        raise BuildError(f"No .sfd files found in {src_dir}")
    return sfd_files


def style_suffix_from_source(path: Path) -> str:
    """Infer style suffix from source filename (e.g. Cartisse-BoldItalic.sfd)."""
    stem = path.stem
    if "-" in stem:
        return stem.split("-")[-1]
    return "Regular"


def ps_name_component(text: str) -> str:
    """Build a PostScript-safe family name component."""
    value = re.sub(r"[^A-Za-z0-9]", "", text)
    if not value:
        value = "Font"
    if value[0].isdigit():
        value = "F" + value
    return value


def safe_filename_component(text: str) -> str:
    """Build a filesystem-safe name component."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return value or "font"


def build_script(
    input_sfd: Path,
    output_ttf: Path,
    family: str,
    style_suffix: str,
    style_display: str,
    ps_weight: str,
    os2_weight: int,
    embolden: float,
    embolden_bold: bool,
    cleanup: bool,
    font_version: str,
    sfnt_revision: float | None,
) -> str:
    """Create the FontForge script for one font file."""
    ps_family = ps_name_component(family)
    ps_fontname = f"{ps_family}-{style_suffix}"
    full_name = f"{family} {style_display}"

    effective_embolden = embolden if (embolden_bold or "Bold" not in style_suffix) else 0.0

    return textwrap.dedent(
        f"""\
        import fontforge

        f = fontforge.open({str(input_sfd)!r})
        print("\\nOpened:", f.fontname)

        EMBOLDEN = {effective_embolden}
        if EMBOLDEN != 0:
            f.selection.all()
            f.changeWeight(EMBOLDEN, "auto", 0, 0)
            print(f"  Applied changeWeight(EMBOLDEN={{EMBOLDEN}})")
        else:
            print("  Embolden skipped for this style (EMBOLDEN=0)")

        CLEANUP = {cleanup!r}
        if CLEANUP:
            f.selection.all()
            f.removeOverlap()
            f.correctDirection()
            f.round()
            print("  Applied cleanup: removeOverlap + correctDirection + round")
        else:
            print("  Skipped cleanup (--skip-cleanup)")

        # Naming/metadata (important when building multiple T-series families).
        f.fontname = {ps_fontname!r}
        f.familyname = {family!r}
        f.fullname = {full_name!r}
        f.weight = {ps_weight!r}

        if hasattr(f, "os2_weight"):
            f.os2_weight = {os2_weight}

        if hasattr(f, "macstyle"):
            macstyle = f.macstyle
            macstyle &= ~((1 << 0) | (1 << 1))
            if "Bold" in {style_suffix!r}:
                macstyle |= (1 << 0)
            if "Italic" in {style_suffix!r}:
                macstyle |= (1 << 1)
            f.macstyle = macstyle

        lang = "English (US)"
        f.appendSFNTName(lang, "Family", {family!r})
        f.appendSFNTName(lang, "SubFamily", {style_display!r})
        f.appendSFNTName(lang, "Fullname", {full_name!r})
        f.appendSFNTName(lang, "PostScriptName", {ps_fontname!r})
        f.appendSFNTName(lang, "Preferred Family", {family!r})
        f.appendSFNTName(lang, "Preferred Styles", {style_display!r})
        f.appendSFNTName(lang, "Compatible Full", {full_name!r})
        f.version = {font_version!r}
        f.appendSFNTName(lang, "Version", {"Version " + font_version!r})

        SFNT_REVISION = {sfnt_revision!r}
        if SFNT_REVISION is not None:
            f.sfntRevision = SFNT_REVISION

        flags = ("opentype", "old-kern", "no-FFTM-table")
        f.generate({str(output_ttf)!r}, flags=flags)
        print("  Exported:", {str(output_ttf)!r})
        print("  Version:", {font_version!r})
        f.close()
        """
    )


def fix_ttf_style_flags(ttf_path: Path, style_suffix: str) -> None:
    """Normalize OS/2 fsSelection and head.macStyle for style linking."""
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        print("  [warn] Skipping style flag fix: fontTools not available", file=sys.stderr)
        return

    font = TTFont(str(ttf_path))
    os2 = font["OS/2"]
    head = font["head"]

    fs_sel = os2.fsSelection
    fs_sel &= ~((1 << 0) | (1 << 5) | (1 << 6))
    if style_suffix == "Regular":
        fs_sel |= (1 << 6)
    if "Italic" in style_suffix:
        fs_sel |= (1 << 0)
    if "Bold" in style_suffix:
        fs_sel |= (1 << 5)
    os2.fsSelection = fs_sel

    macstyle = 0
    if "Bold" in style_suffix:
        macstyle |= (1 << 0)
    if "Italic" in style_suffix:
        macstyle |= (1 << 1)
    head.macStyle = macstyle

    font.save(str(ttf_path))
    font.close()
    print(f"  Normalized style flags for {style_suffix}")


def add_kern_pairs(ttf_path: Path) -> None:
    """Prepend explicit kern pairs to the first PairPos(Format 1) table."""
    if not KERN_PAIRS:
        return

    try:
        from fontTools.ttLib import TTFont
        from fontTools.ttLib.tables.otTables import PairValueRecord, ValueRecord, PairSet
    except Exception:
        print("  [warn] Skipping kern pairs: fontTools not available", file=sys.stderr)
        return

    font = TTFont(str(ttf_path))
    gpos = font.get("GPOS")
    if gpos is None:
        font.close()
        print("  [warn] No GPOS table, skipping kern pairs", file=sys.stderr)
        return

    cmap = font.getBestCmap()
    glyph_order = set(font.getGlyphOrder())

    def resolve(name: str) -> str | None:
        if len(name) == 1:
            cp = ord(name)
            if cp in cmap:
                return cmap[cp]
        if name in glyph_order:
            return name
        return None

    pairs: list[tuple[str, str, int]] = []
    for left, right, value in KERN_PAIRS:
        l = resolve(left)
        r = resolve(right)
        if l and r:
            pairs.append((l, r, value))

    if not pairs:
        font.close()
        return

    pair_pos = None
    for lookup in gpos.table.LookupList.Lookup:
        if lookup.LookupType == 2:
            for subtable in lookup.SubTable:
                if subtable.Format == 1:
                    pair_pos = subtable
                    break
            if pair_pos:
                break

    if pair_pos is None:
        font.close()
        print("  [warn] No PairPos Format 1 table, skipping kern pairs", file=sys.stderr)
        return

    count = 0
    for left_glyph, right_glyph, value in pairs:
        try:
            idx = pair_pos.Coverage.glyphs.index(left_glyph)
        except ValueError:
            pair_pos.Coverage.glyphs.append(left_glyph)
            ps = PairSet()
            ps.PairValueRecord = []
            ps.PairValueCount = 0
            pair_pos.PairSet.append(ps)
            pair_pos.PairSetCount = len(pair_pos.PairSet)
            idx = len(pair_pos.Coverage.glyphs) - 1

        pair_set = pair_pos.PairSet[idx]
        pair_set.PairValueRecord = [
            pvr for pvr in pair_set.PairValueRecord if pvr.SecondGlyph != right_glyph
        ]

        pvr = PairValueRecord()
        pvr.SecondGlyph = right_glyph
        vr = ValueRecord()
        vr.XAdvance = value
        pvr.Value1 = vr
        pair_set.PairValueRecord.insert(0, pvr)
        pair_set.PairValueCount = len(pair_set.PairValueRecord)
        count += 1

    font.save(str(ttf_path))
    font.close()
    print(f"  Added {count} kern pair(s) to GPOS")


def check_ttfautohint(required: bool) -> None:
    """Ensure ttfautohint is available when required."""
    if shutil.which("ttfautohint"):
        return
    if not required:
        return

    raise BuildError(
        "ttfautohint not found. Install it for Kobo-ready hinting, "
        "or rerun with --skip-autohint."
    )


def autohint_ttf(ttf_path: Path) -> None:
    """Run ttfautohint on a TTF in-place."""
    if not shutil.which("ttfautohint"):
        raise BuildError("ttfautohint not found")

    tmp_path = str(ttf_path) + ".autohint.tmp"
    result = subprocess.run(
        ["ttfautohint"] + AUTOHINT_OPTS + [str(ttf_path), tmp_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise BuildError(f"ttfautohint failed for {ttf_path.name}: {err}")

    os.replace(tmp_path, str(ttf_path))
    print("  Autohinted with ttfautohint")


def resolve_kobofix_script(
    explicit_path: str | None,
    cache_dir: Path,
) -> Path:
    """Get kobofix.py from explicit path or download/cache it."""
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        if not candidate.is_file():
            raise BuildError(f"kobofix.py not found at {candidate}")
        return candidate

    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / "kobofix.py"
    if cached.is_file():
        print("  Using cached kobofix.py")
        return cached

    print("  Downloading kobofix.py ...")
    try:
        urllib.request.urlretrieve(KOBOFIX_URL, str(cached))
    except Exception as exc:
        raise BuildError(
            "Could not download kobofix.py. Pass --kobofix-path /path/to/kobofix.py "
            "or rerun with --skip-kobo-fix."
        ) from exc

    print(f"  Saved to {cached}")
    return cached


def run_kobofix(kobofix_path: Path, ttf_paths: list[Path], out_kf_dir: Path) -> None:
    """Run kobofix preset on fonts and move KF_* results to out/kf."""
    if not ttf_paths:
        return

    cmd = [sys.executable, str(kobofix_path), "--preset", "kf"] + [
        str(path) for path in ttf_paths
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if "skia-pathops" in err:
            err += "\nInstall with: python3 -m pip install skia-pathops"
        raise BuildError(f"kobofix.py failed: {err}")

    out_kf_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    missing = 0

    for ttf_path in ttf_paths:
        kf_name = f"KF_{ttf_path.name}"
        src = ttf_path.parent / kf_name
        if src.is_file():
            shutil.move(str(src), str(out_kf_dir / kf_name))
            moved += 1
        else:
            missing += 1

    print(f"  Moved {moved} KF font(s) to {out_kf_dir}")
    if missing:
        print(
            f"  [warn] Expected {missing} KF file(s) were not generated",
            file=sys.stderr,
        )


def build_targets(args: argparse.Namespace) -> list[BuildTarget]:
    """Resolve build targets from CLI arguments."""
    if args.t_series:
        start, end = args.t_series
        if end < start:
            raise BuildError("--t-series END must be >= START")
        if args.t_step <= 0:
            raise BuildError("--t-step must be a positive integer")

        targets: list[BuildTarget] = []
        for value in range(start, end + 1, args.t_step):
            targets.append(BuildTarget(family=f"{args.family} T{value}", embolden=float(value)))
        return targets

    return [BuildTarget(family=args.family, embolden=args.embolden)]


def build_fonts(
    src_dir: Path,
    out_dir: Path,
    targets: list[BuildTarget],
    fontforge_cmd: list[str],
    clean: bool,
    cleanup: bool,
    only_fonts: set[str] | None,
    verbose_fontforge: bool,
    skip_autohint: bool,
    skip_kobo_fix: bool,
    kobofix_path: str | None,
    embolden_bold: bool,
    font_version: str,
    sfnt_revision: float | None,
) -> None:
    """Run the build pipeline for all targets and source files."""
    sources = collect_sources(src_dir, only_fonts)
    check_ttfautohint(required=not skip_autohint)

    out_ttf_dir = out_dir / "ttf"
    out_kf_dir = out_dir / "kf"

    if clean and out_dir.exists():
        shutil.rmtree(out_dir)

    out_ttf_dir.mkdir(parents=True, exist_ok=True)
    if not skip_kobo_fix:
        out_kf_dir.mkdir(parents=True, exist_ok=True)

    kobofix_cache = Path(tempfile.gettempdir()) / "cartisse-kobofix"
    resolved_kobofix = None
    if not skip_kobo_fix:
        resolved_kobofix = resolve_kobofix_script(kobofix_path, kobofix_cache)

    print("=" * 60)
    print("Cartisse Build")
    print("=" * 60)
    print(f"Source dir : {src_dir}")
    print(f"Output dir : {out_dir}")
    print(f"Targets    : {len(targets)}")
    print(f"Version    : {font_version}")
    print(f"Bold embolden: {'yes' if embolden_bold else 'no'}")
    print(f"Cleanup    : {'yes' if cleanup else 'no'}")
    print(f"Autohint   : {'yes' if not skip_autohint else 'no'}")
    print(f"Kobo fix   : {'yes' if not skip_kobo_fix else 'no'}")
    print(f"FontForge  : {' '.join(fontforge_cmd)}")

    generated_ttf = 0
    generated_kf = 0

    for target in targets:
        print("\n" + "-" * 60)
        print(f"Family: {target.family}")
        print(f"Embolden: {target.embolden}")
        print("-" * 60)

        target_ttf_paths: list[Path] = []
        family_file_component = safe_filename_component(target.family)

        for src in sources:
            style_suffix = style_suffix_from_source(src)
            style_display, ps_weight, os2_weight = STYLE_MAP.get(
                style_suffix,
                (style_suffix, "Book", 400),
            )

            out_name = f"{family_file_component}-{style_suffix}.ttf"
            out_ttf = out_ttf_dir / out_name

            print(f"\nProcessing {src.name} -> {out_name}")
            script = build_script(
                input_sfd=src,
                output_ttf=out_ttf,
                family=target.family,
                style_suffix=style_suffix,
                style_display=style_display,
                ps_weight=ps_weight,
                os2_weight=os2_weight,
                embolden=target.embolden,
                embolden_bold=embolden_bold,
                cleanup=cleanup,
                font_version=font_version,
                sfnt_revision=sfnt_revision,
            )
            run_fontforge_script(
                fontforge_cmd,
                script,
                verbose_fontforge=verbose_fontforge,
            )

            fix_ttf_style_flags(out_ttf, style_suffix)
            add_kern_pairs(out_ttf)
            if not skip_autohint:
                autohint_ttf(out_ttf)

            target_ttf_paths.append(out_ttf)
            generated_ttf += 1

        if not skip_kobo_fix and resolved_kobofix is not None:
            print("\nApplying kobofix preset kf ...")
            before = {p.name for p in out_kf_dir.glob("KF_*.ttf")}
            run_kobofix(resolved_kobofix, target_ttf_paths, out_kf_dir)
            after = {p.name for p in out_kf_dir.glob("KF_*.ttf")}
            generated_kf += len(after - before)

    print("\n" + "=" * 60)
    print("Build complete.")
    print(f"Generated TTF files: {generated_ttf} -> {out_ttf_dir}")
    if not skip_kobo_fix:
        print(f"Generated KF files:  {generated_kf} -> {out_kf_dir}")
    print("=" * 60)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build Cartisse from SFD sources with tunable embolden and Kobo-ready output."
        )
    )
    parser.add_argument(
        "--src-dir",
        default=str(DEFAULT_SRC_DIR),
        help="Directory containing source .sfd files (default: ./src)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory (default: ./out)",
    )
    parser.add_argument(
        "--family",
        default=DEFAULT_FAMILY,
        help="Base family name (default: Cartisse)",
    )
    parser.add_argument(
        "--embolden",
        type=float,
        default=DEFAULT_EMBOLDEN,
        help=f"Single-build stem thickening in font units (default: {DEFAULT_EMBOLDEN:g})",
    )
    parser.add_argument(
        "--embolden-bold",
        action="store_true",
        help="Also apply embolden to Bold/BoldItalic styles (default: off).",
    )
    parser.add_argument(
        "--version-file",
        default=str(DEFAULT_VERSION_FILE),
        help="Path to VERSION file used for output font version metadata.",
    )
    parser.add_argument(
        "--t-series",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help=(
            "Build T-series variants. Example: --t-series 8 20 creates "
            "'Cartisse T8' ... 'Cartisse T20' with matching embolden values."
        ),
    )
    parser.add_argument(
        "--t-step",
        type=int,
        default=1,
        help="Step size for --t-series (default: 1)",
    )
    parser.add_argument(
        "--fonts",
        nargs="+",
        default=None,
        help=(
            "Optional subset of source fonts by stem or filename. "
            "Example: --fonts Cartisse-Regular Cartisse-Italic.sfd"
        ),
    )
    parser.add_argument(
        "--fontforge",
        default=None,
        help="Path to FontForge binary (optional)",
    )
    parser.add_argument(
        "--kobofix-path",
        default=None,
        help="Path to local kobofix.py (optional; otherwise downloaded/cached)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete output directory before building.",
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Skip overlap/direction/round cleanup.",
    )
    parser.add_argument(
        "--skip-autohint",
        action="store_true",
        help="Skip ttfautohint post-processing.",
    )
    parser.add_argument(
        "--skip-kobo-fix",
        action="store_true",
        help="Skip kobofix KF variant generation.",
    )
    parser.add_argument(
        "--verbose-fontforge",
        action="store_true",
        help="Show full raw FontForge stderr output (very noisy).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        targets = build_targets(args)
        font_version = load_font_version(Path(args.version_file).resolve())
        sfnt_revision = parse_sfnt_revision(font_version)
        fontforge_cmd = find_fontforge(args.fontforge)
        build_fonts(
            src_dir=Path(args.src_dir).resolve(),
            out_dir=Path(args.out_dir).resolve(),
            targets=targets,
            fontforge_cmd=fontforge_cmd,
            clean=args.clean,
            cleanup=not args.skip_cleanup,
            only_fonts=set(args.fonts) if args.fonts else None,
            verbose_fontforge=args.verbose_fontforge,
            skip_autohint=args.skip_autohint,
            skip_kobo_fix=args.skip_kobo_fix,
            kobofix_path=args.kobofix_path,
            embolden_bold=args.embolden_bold,
            font_version=font_version,
            sfnt_revision=sfnt_revision,
        )
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
