"""`/uploads` — ce qu'un dépôt écrit, et ce qu'il refuse d'écrire.

La route rend un chemin ; tout ce qui compte tient dans la question « ce chemin
désigne-t-il bien le contenu envoyé, et rien d'autre ». D'où l'ordre de ce
fichier : le cas nominal, puis les trois façons de sortir du sas — un type que
le registre n'accepte pas, une taille qui déborde, un nom qui remonte l'arbre.
"""

import io

import pytest
from ecurie_api.uploads import (
    MAX_BYTES,
    UploadError,
    UploadTooLarge,
    accepted_media_types,
    correspond,
    deposer,
    nom_sur_disque,
    purger,
)
from ecurie_core.capabilities import CapabilityContract


def _envoyer(client, contenu: bytes, *, nom: str = "photo.png", type_media: str = "image/png"):
    return client.post("/uploads", files={"file": (nom, io.BytesIO(contenu), type_media)})


@pytest.fixture
def client_images(client_factory, depot):
    """Un dépôt dont un contrat accepte des images — et un autre, du son."""
    return client_factory(
        depot.capability("image-to-image")
        .capability("audio-to-text")
        .env("diffusers-mps")
        .model(capability="image-to-image", runtime="diffusers-mps")
    )


# --- le cas nominal --------------------------------------------------------------


def test_un_depot_rend_un_chemin_qui_porte_le_contenu_envoye(client_images, ecurie_home):
    réponse = _envoyer(client_images, b"\x89PNG" + b"0" * 128)

    assert réponse.status_code == 201, réponse.text
    corps = réponse.json()
    assert corps["media_type"] == "image/png"
    assert corps["size_bytes"] == 132

    écrit = ecurie_home / "uploads" / corps["name"]
    assert écrit.read_bytes() == b"\x89PNG" + b"0" * 128
    # Le chemin rendu est celui-là même, en absolu : c'est ce qu'un champ
    # `x-ui: "file"` porte, et ce que `ecurie run -p image=…` attend.
    assert corps["path"] == str(écrit)


def test_deux_depots_du_meme_nom_ne_se_marchent_pas_dessus(client_images):
    """Deux captures s'appellent toutes les deux `enregistrement.wav`."""
    premier = _envoyer(client_images, b"A" * 16).json()
    second = _envoyer(client_images, b"B" * 32).json()

    assert premier["name"] != second["name"]
    assert premier["path"] != second["path"]
    assert second["size_bytes"] == 32


def test_une_capture_sans_extension_recoit_celle_de_son_type(client_images):
    """Un `Blob` de capture s'envoie sous le nom « blob » : il n'a qu'un type.

    Et `audio/wav` est justement celui que `mimetypes` ne sait pas suffixer.
    """
    réponse = client_images.post(
        "/uploads", files={"file": ("blob", io.BytesIO(b"RIFF...."), "audio/wav")}
    )

    assert réponse.status_code == 201, réponse.text
    assert réponse.json()["name"].endswith(".wav")


# --- ce que le registre refuse -----------------------------------------------------


def test_un_type_qu_aucun_contrat_n_accepte_est_refuse(client_images):
    réponse = client_images.post(
        "/uploads", files={"file": ("script.sh", io.BytesIO(b"rm -rf /"), "application/x-sh")}
    )

    assert réponse.status_code == 415
    assert "application/x-sh" in réponse.text
    # Le refus dit ce qui **serait** accepté : sans cela, il faut aller lire les
    # contrats un par un pour deviner.
    assert "image/*" in réponse.text


def test_le_registre_decide_et_non_une_liste_ecrite_dans_le_code(client_factory, depot):
    """Un dépôt dont aucun contrat n'a de champ fichier n'accepte rien.

    C'est la propriété qui rend la vérification opposable : elle suit le
    registre, elle ne le double pas.
    """
    client = client_factory(
        depot.capability("text-to-image").env("diffusers-mps").model(
            capability="text-to-image", runtime="diffusers-mps"
        )
    )

    réponse = _envoyer(client, b"\x89PNG")

    assert réponse.status_code == 415
    assert "aucun type" in réponse.text


def test_un_depot_trop_gros_est_interrompu_et_ne_laisse_rien(
    client_images, ecurie_home, monkeypatch
):
    """La borne est celle du module, et la route la lit à l'appel — pas à l'import."""
    from ecurie_api import uploads as module

    monkeypatch.setattr(module, "MAX_BYTES", 64)

    réponse = _envoyer(client_images, b"\x89PNG" + b"0" * 512)

    assert réponse.status_code == 413
    assert list((ecurie_home / "uploads").iterdir()) == []


# --- les fonctions, isolées --------------------------------------------------------


def test_un_nom_ne_peut_jamais_sortir_du_dossier():
    nom = nom_sur_disque("../../.ssh/authorized_keys", "image/png", jeton="20260822-120000-abcdef")

    assert "/" not in nom
    assert nom.startswith("20260822-120000-abcdef-")


def test_un_nom_vide_reste_un_nom():
    assert nom_sur_disque("", "audio/wav", jeton="J") == "J-depot.wav"
    assert nom_sur_disque("...", "audio/wav", jeton="J") == "J-depot.wav"


def test_un_nom_deja_suffixe_garde_son_extension():
    assert nom_sur_disque("prise 2.WAV", "audio/wav", jeton="J") == "J-prise_2.WAV"


@pytest.mark.parametrize(
    ("type_media", "motif", "attendu"),
    [
        ("image/png", "image/*", True),
        ("image/png", "audio/*", False),
        ("audio/webm;codecs=opus", "audio/*", True),
        ("APPLICATION/PDF", "application/pdf", True),
        ("n'importe/quoi", "*/*", True),
        ("imagerie/png", "image/*", False),
    ],
)
def test_la_correspondance_suit_la_graphie_de_accept(type_media, motif, attendu):
    assert correspond(type_media, motif) is attendu


def test_les_motifs_acceptes_viennent_des_contrats():
    contrat = CapabilityContract(
        "essai",
        {
            "input": {
                "properties": {
                    "doc": {"type": "string", "contentMediaType": "application/pdf,image/*"},
                    "libre": {"type": "string", "x-ui": "file"},
                    "texte": {"type": "string"},
                }
            }
        },
    )

    assert accepted_media_types([contrat]) == ["*/*", "application/pdf", "image/*"]


def test_la_purge_ne_retire_que_ce_qui_a_depasse_la_retention(tmp_path):
    import os

    sas = tmp_path / "uploads"
    sas.mkdir()
    vieux, récent = sas / "vieux.png", sas / "recent.png"
    vieux.write_bytes(b"v")
    récent.write_bytes(b"r")
    os.utime(vieux, (0, 0))

    assert purger(sas, retention_s=3600) == 1
    assert not vieux.exists()
    assert récent.exists()


def test_la_purge_d_un_dossier_absent_ne_leve_pas(tmp_path):
    assert purger(tmp_path / "jamais-cree") == 0


def test_deposer_refuse_avant_d_ecrire_quoi_que_ce_soit(tmp_path):
    sas = tmp_path / "uploads"

    with pytest.raises(UploadError):
        deposer(
            io.BytesIO(b"x"),
            dossier=sas,
            nom_origine="a.bin",
            media_type="application/octet-stream",
            motifs=["image/*"],
        )

    assert not sas.exists() or list(sas.iterdir()) == []


def test_deposer_supprime_le_fichier_partiel_au_depassement(tmp_path):
    sas = tmp_path / "uploads"

    with pytest.raises(UploadTooLarge):
        deposer(
            io.BytesIO(b"0" * 4096),
            dossier=sas,
            nom_origine="gros.png",
            media_type="image/png",
            motifs=["image/*"],
            max_bytes=128,
        )

    assert list(sas.iterdir()) == []


def test_la_borne_par_defaut_est_celle_du_module(tmp_path):
    """Un gigaoctet : une vidéo à transcrire passe, un disque ne se remplit pas."""
    assert MAX_BYTES == 1 << 30
