"""Adaptateur mlx-vlm — ce qui se vérifie sans Apple Silicon ni mlx.

Ces tests tournent dans le venv d'Écurie, qui n'a ni mlx ni mlx-vlm : c'est la
situation de la CI, et c'est ce qui leur donne leur valeur. Ils couvrent ce qui
casse en silence — un import remonté au niveau du module, un `page_range` mal
interprété, un chemin de document résolu au mauvais endroit — et laissent au
banc d'essai ce qui demande un vrai modèle.
"""

import pytest
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.mlx_vlm import (
    CONSIGNES,
    DEFAULT_DPI,
    MAX_PAGES,
    MlxVlmWorker,
    build_prompt,
    import_runtime,
    parse_page_range,
    plan_pages,
    resolve_document,
)


def test_le_module_s_importe_sans_mlx():
    """La CI n'a pas Apple Silicon : un import remonté au niveau du module
    ferait échouer la collecte des tests, pas seulement ce worker."""
    assert MlxVlmWorker.name == "mlx-vlm"


def test_l_absence_du_runtime_nomme_la_reparation(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "mlx_vlm", None)
    with pytest.raises(WorkerError) as exc:
        import_runtime()
    assert "ecurie env sync mlx-vlm" in str(exc.value)


# --- page_range ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("brut", "total", "attendu"),
    [
        (None, 3, [1, 2, 3]),
        ("", 3, [1, 2, 3]),
        ("   ", 2, [1, 2]),
        ("2", 5, [2]),
        ("1-3", 5, [1, 2, 3]),
        ("1-2,4", 5, [1, 2, 4]),
        ("4,1-2", 5, [1, 2, 4]),  # remis en ordre
        ("1-2,2-3", 5, [1, 2, 3]),  # dédupliqué
        ("1-999", 3, [1, 2, 3]),  # borne haute rabotée : « 1-999 » veut dire « tout »
        ("0-2", 4, [1, 2]),  # les pages commencent à 1
    ],
)
def test_page_range(brut, total, attendu):
    assert parse_page_range(brut, total) == attendu


def test_page_range_illisible():
    with pytest.raises(WorkerError, match="illisible"):
        parse_page_range("deux", 5)


def test_page_range_hors_document():
    """Refuser vaut mieux que rendre un document vide : l'utilisateur croirait
    que la page est blanche alors qu'elle n'a jamais été lue."""
    with pytest.raises(WorkerError, match="ne désigne aucune page"):
        parse_page_range("7-9", 3)


# --- document -----------------------------------------------------------------------


def test_un_chemin_relatif_se_resout_dans_le_dossier_du_job(tmp_path):
    """Le superviseur copie l'entrée dans le job et transmet un chemin relatif :
    c'est ce qui rend le job rejouable ailleurs que sur ce disque."""
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "page.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert resolve_document("inputs/page.png", tmp_path) == tmp_path / "inputs" / "page.png"


def test_un_chemin_absolu_reste_accepte(tmp_path):
    cible = tmp_path / "ailleurs.png"
    cible.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert resolve_document(str(cible), tmp_path / "job") == cible


def test_document_vide_ou_absent(tmp_path):
    with pytest.raises(WorkerError, match="est vide"):
        resolve_document("", tmp_path)
    with pytest.raises(WorkerError, match="introuvable"):
        resolve_document("nulle-part.png", tmp_path)


def test_une_image_est_une_page_unique(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    plans = plan_pages(image, tmp_path, dpi=DEFAULT_DPI, page_range=None)
    assert [p.numero for p in plans] == [1]
    assert plans[0].image == image


def test_page_range_sur_une_image_est_refuse(tmp_path):
    """Ignorer le paramètre laisserait croire qu'il a été honoré."""
    image = tmp_path / "page.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(WorkerError, match="page unique"):
        plan_pages(image, tmp_path, dpi=DEFAULT_DPI, page_range="2-3")


def test_format_non_gere(tmp_path):
    doc = tmp_path / "notes.docx"
    doc.write_bytes(b"PK")
    with pytest.raises(WorkerError) as exc:
        plan_pages(doc, tmp_path, dpi=DEFAULT_DPI, page_range=None)
    assert ".docx" in str(exc.value) and "PDF" in str(exc.value)


# --- consigne -----------------------------------------------------------------------


def test_la_consigne_suit_le_format_demande():
    assert "Markdown" in build_prompt("markdown", None, True)
    assert "sans mise en forme" in build_prompt("text", None, True)
    # Un format inconnu retombe sur le plus riche plutôt que d'échouer : le
    # contrat borne déjà l'enum, c'est une ceinture.
    assert build_prompt("inconnu", None, True) == build_prompt("markdown", None, True)


def test_la_langue_est_transmise_quand_elle_est_dite():
    assert "en français" in build_prompt("markdown", "français", True)
    for muet in (None, "", "  ", "auto", "AUTO"):
        assert "Le document est en" not in build_prompt("markdown", muet, True)


def test_sans_detect_layout_la_consigne_le_dit():
    assert "Ignore la mise en page" in build_prompt("markdown", None, False)
    assert "Ignore la mise en page" not in build_prompt("markdown", None, True)


def test_les_consignes_couvrent_l_enum_du_contrat(repo_root):
    """Une dérive entre le contrat et l'adaptateur ne se verrait qu'à l'exécution."""
    import json

    contrat = json.loads(
        (repo_root / "registry" / "capabilities" / "document-to-text.json").read_text()
    )
    assert set(contrat["input"]["properties"]["format"]["enum"]) == set(CONSIGNES)


# --- réglages -----------------------------------------------------------------------


def _requete(**entree) -> InferRequest:
    from pathlib import Path

    return InferRequest(job_id="j", input=entree, params={}, output_dir=Path("."))


def test_l_ordre_des_reglages_entree_options_defauts():
    worker = MlxVlmWorker()
    worker._defaults = {"dpi": 150, "format": "text"}
    worker._options = {"dpi": 300}

    # L'entrée du job tranche toujours.
    assert worker._reglage(_requete(dpi=72), "dpi", DEFAULT_DPI) == 72
    # Sinon les options du variant, puis ses défauts.
    assert worker._reglage(_requete(), "dpi", DEFAULT_DPI) == 300
    assert worker._reglage(_requete(), "format", "markdown") == "text"
    # Et à défaut de tout, la valeur de l'adaptateur.
    assert worker._reglage(_requete(), "inconnu", "repli") == "repli"


def test_le_plafond_de_pages_est_annonce():
    """Un document de 400 pages n'est pas un job, c'est une file d'attente."""
    assert MAX_PAGES == 20
