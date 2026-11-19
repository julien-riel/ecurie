"""Copie le portage MLX de SoulX-FlashHead sous `vendor/`, au commit épinglé.

    python3 runtimes/mlx-flashhead/vendorer.py

**Pourquoi un script.** Le portage vit dans un sous-dossier d'un dépôt plus large
(github.com/Avdpro/ai2apps, `packages/ai2apps-model-flashhead-mlx/src/flashhead_mlx`)
et n'a pas de `pyproject.toml`. `uv` ne sait donc pas l'installer depuis Git. Le
script fait un clone partiel au commit épinglé et copie le module tel quel, sans en
modifier une ligne.

**L'empreinte est vérifiée.** Le profil du manifeste a été mesuré sur ce code-là.
Une empreinte différente veut dire que l'on n'exécute plus ce qui a été mesuré : le
script refuse, sauf `--accepter-ecart`, et c'est alors à l'humain de relire la
différence.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent
DEPOT = "https://github.com/Avdpro/ai2apps.git"
COMMIT = "39ef33b2e71bb2e4c227f4ae539f65601d456392"  # 2026-10-28
PAQUET = "packages/ai2apps-model-flashhead-mlx"
MODULE = "flashhead_mlx"
CIBLE = RACINE / "vendor"

#: sha256 de la concaténation (chemin, contenu) des fichiers `.py` du module, triés.
SHA256_EPROUVE = "001bb6b320fcfbe062ddfd31521dec441624ea4c2c08d412f4abba5efbe810b9"


def empreinte(dossier: Path) -> str:
    h = hashlib.sha256()
    for fichier in sorted(dossier.rglob("*.py")):
        h.update(fichier.relative_to(dossier).as_posix().encode())
        h.update(b"\0")
        h.update(fichier.read_bytes())
    return h.hexdigest()


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parseur.add_argument("--accepter-ecart", action="store_true")
    args = parseur.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "ai2apps"
        git("clone", "--quiet", "--filter=blob:none", "--no-checkout", DEPOT, str(clone))
        git("sparse-checkout", "set", PAQUET, cwd=clone)
        git("checkout", "--quiet", COMMIT, cwd=clone)
        source = clone / PAQUET / "src" / MODULE
        if not source.is_dir():
            raise SystemExit(f"{PAQUET}/src/{MODULE} absent au commit {COMMIT}")
        obtenue = empreinte(source)
        if obtenue != SHA256_EPROUVE and not args.accepter_ecart:
            raise SystemExit(
                f"empreinte {obtenue}\nattendue {SHA256_EPROUVE}\n"
                "Le code n'est plus celui qui a été mesuré. Relire la différence, puis "
                "relancer avec --accepter-ecart."
            )
        destination = CIBLE / MODULE
        if destination.exists():
            shutil.rmtree(destination)
        CIBLE.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(clone / PAQUET / "LICENSE.upstream", CIBLE / "LICENSE")
        (CIBLE / "COMMIT").write_text(f"{DEPOT}@{COMMIT}\n", encoding="utf-8")

    print(f"écrit  : {destination}")
    print(f"sha256 : {obtenue}")
    if obtenue != SHA256_EPROUVE:
        print("ATTENTION — empreinte différente de celle éprouvée.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
