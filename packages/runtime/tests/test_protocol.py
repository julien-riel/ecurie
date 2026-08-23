"""Le protocole worker : vocabulaire, ligne unique, plafond (CONCEPTION.md §5.1).

Deux promesses de la conception se vérifient ici plus que le reste : un message
tient sur **une seule ligne** — c'est ce qui permet au canal de séparer les
messages sur un simple saut de ligne — et une sortie volumineuse ne traverse pas
le canal, elle passe par un fichier dans `output_dir`.

Ce module est importé des deux côtés, dont un venv isolé qui ne connaît rien
d'Écurie : rien ici n'a besoin d'un worker, d'un tube ni d'un modèle.
"""

import json

import pytest
from ecurie_runtime.protocol import (
    EVENTS,
    MAX_LINE_BYTES,
    OPS,
    ProtocolError,
    decode,
    encode,
    ev,
    is_terminal,
    kind_of,
    name_of,
    op,
)


def _resultat_de(octets: int) -> dict:
    """Un `result` dont la ligne encodée pèse exactement `octets`, saut de ligne exclu."""
    gabarit = ev("result", job_id="j1", output={"blob": ""})
    creux = len(json.dumps(gabarit, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    gabarit["output"]["blob"] = "x" * (octets - creux)
    return gabarit


# --- vocabulaire ------------------------------------------------------------


def test_le_vocabulaire_est_celui_de_la_conception():
    """Ces chaînes-là voyagent sur le canal. Les renommer casse tout worker déjà
    installé dans son venv, y compris ceux qu'Écurie n'a pas écrits."""
    assert OPS == {"load", "infer", "unload", "ping"}
    assert EVENTS == {"loaded", "progress", "delta", "result", "error", "pong", "unloaded"}


def test_op_construit_une_operation_avec_ses_champs():
    assert op("ping") == {"op": "ping"}
    assert op("infer", job_id="j1", input={"text": "bonjour"}, seed=42) == {
        "op": "infer",
        "job_id": "j1",
        "input": {"text": "bonjour"},
        "seed": 42,
    }


def test_ev_construit_un_evenement_avec_ses_champs():
    assert ev("unloaded") == {"ev": "unloaded"}
    assert ev("loaded", warmup_ms=2400, peak_memory_bytes=3_100_000_000) == {
        "ev": "loaded",
        "warmup_ms": 2400,
        "peak_memory_bytes": 3_100_000_000,
    }


def test_op_refuse_un_nom_qui_n_est_pas_une_operation():
    with pytest.raises(ProtocolError, match="opération inconnue"):
        op("charger")
    # `loaded` est un événement : le superviseur ne l'émet pas, il l'attend.
    with pytest.raises(ProtocolError, match="opération inconnue"):
        op("loaded")


def test_ev_refuse_un_nom_qui_n_est_pas_un_evenement():
    with pytest.raises(ProtocolError, match="événement inconnu"):
        ev("termine")
    with pytest.raises(ProtocolError, match="événement inconnu"):
        ev("infer")


# --- une ligne, et rien qu'une ----------------------------------------------


def test_aller_retour_sur_un_message_accentue():
    message = ev(
        "error",
        job_id="j1",
        message="échec du chargement : « modèle » introuvable",
        trace="Fichier « générateur.py », ligne 12\n  ré-essai…\n",
        output={"note": "音声 · 3D"},
    )
    ligne = encode(message)

    assert decode(ligne) == message
    assert decode(ligne.decode("utf-8")) == message
    # `ensure_ascii=False` : les accents voyagent tels quels. Les échapper
    # gonflerait d'un tiers chaque message d'un worker francophone.
    assert "« modèle »".encode() in ligne
    assert b"\\u" not in ligne


def test_encode_ne_produit_qu_une_seule_ligne():
    """Le canal découpe sur les sauts de ligne : une trace Python écrite telle
    quelle couperait le message en deux, et sa seconde moitié passerait pour du bruit."""
    message = ev("error", job_id="j1", message="ligne 1\nligne 2", trace="a\nb\r\nc\n")
    ligne = encode(message)

    assert ligne.count(b"\n") == 1
    assert ligne.endswith(b"\n")
    assert decode(ligne)["trace"] == "a\nb\r\nc\n"


# --- ce qui n'est pas un message --------------------------------------------


def test_encode_refuse_ce_qui_n_est_pas_un_message():
    with pytest.raises(ProtocolError, match="sans clé"):
        encode({"job_id": "j1", "pct": 40})
    # Les deux clés à la fois se refusent aussi, mais pour une autre raison :
    # le message n'est pas incomplet, il est ambigu, et le dire ainsi évite de
    # chercher une clé absente qui est en fait présente deux fois.
    with pytest.raises(ProtocolError, match="à la fois"):
        encode({"op": "ping", "ev": "pong"})


@pytest.mark.parametrize(
    ("ligne", "fragment"),
    [
        (b'{"job_id":"j1","pct":40}', "sans clé"),
        (b'[{"op":"ping"}]', "n'est pas un objet"),
        (b'"pong"', "n'est pas un objet"),
        (b"42", "n'est pas un objet"),
        (b"", "ligne vide"),
        (b"\n", "ligne vide"),
        (b"   \t \r\n", "ligne vide"),
        (b'{"op":"ping"', "JSON illisible"),
        (b"chargement des poids : 40 %", "JSON illisible"),
        (b'{"ev":"error","message":"\xe9chec"}', "non-UTF-8"),
    ],
    ids=[
        "sans-op-ni-ev",
        "tableau",
        "chaine",
        "nombre",
        "vide",
        "saut-de-ligne-seul",
        "blancs",
        "json-tronque",
        "texte-brut",
        "latin-1",
    ],
)
def test_decode_refuse_ce_qui_n_est_pas_un_message(ligne, fragment):
    with pytest.raises(ProtocolError, match=fragment):
        decode(ligne)


def test_decode_refuse_un_message_qui_porte_op_et_ev():
    """Le canal est bidirectionnel : deviner de quel côté va un message ambigu,
    c'est risquer de traiter une opération comme un résultat."""
    assert kind_of({"op": "ping", "ev": "pong"}) is None
    with pytest.raises(ProtocolError):
        decode(b'{"op":"ping","ev":"pong"}')


# --- plafond de ligne --------------------------------------------------------


def test_le_plafond_renvoie_l_auteur_du_worker_vers_output_dir():
    """Le refus doit dire quoi faire : c'est l'auteur d'un adaptateur qui le lit,
    au moment précis où il essaie de renvoyer un maillage encodé en base64."""
    maillage = ev("result", job_id="j1", output={"mesh_b64": "A" * (2 * MAX_LINE_BYTES)})

    with pytest.raises(ProtocolError) as exc:
        encode(maillage)
    assert "output_dir" in str(exc.value)
    assert "fichiers" in str(exc.value)


def test_le_plafond_accepte_la_ligne_maximale_et_refuse_l_octet_suivant():
    assert len(encode(_resultat_de(MAX_LINE_BYTES))) == MAX_LINE_BYTES + 1  # + le saut de ligne

    with pytest.raises(ProtocolError) as exc:
        encode(_resultat_de(MAX_LINE_BYTES + 1))
    assert f"{MAX_LINE_BYTES + 1} octets" in str(exc.value)


def test_le_plafond_compte_les_octets_et_non_les_caracteres():
    """« é » pèse deux octets : un plafond mesuré sur la chaîne laisserait passer
    presque le double de ce que le superviseur accepte de mettre en mémoire."""
    texte = "é" * (MAX_LINE_BYTES // 2)
    assert len(texte) < MAX_LINE_BYTES

    with pytest.raises(ProtocolError, match="output_dir"):
        encode(ev("error", job_id="j1", message=texte))


# --- lecture d'un message ----------------------------------------------------


def test_kind_of_dit_le_sens_du_message():
    assert kind_of({"op": "load"}) == "op"
    assert kind_of({"ev": "loaded"}) == "ev"
    assert kind_of({"job_id": "j1"}) is None
    assert kind_of({}) is None


def test_name_of_rend_le_nom_du_message():
    assert name_of({"op": "infer", "job_id": "j1"}) == "infer"
    assert name_of({"ev": "progress", "pct": 40}) == "progress"
    with pytest.raises(ProtocolError, match="sans clé"):
        name_of({"pct": 40})


def test_name_of_refuse_un_nom_qui_n_est_pas_une_chaine():
    """Sans ce garde-fou, `name_of` rendrait un entier et toutes les comparaisons
    de nom en aval seraient fausses en silence."""
    with pytest.raises(ProtocolError, match="pas une chaîne"):
        name_of({"ev": 3})
    with pytest.raises(ProtocolError, match="pas une chaîne"):
        name_of({"op": None})


def test_is_terminal_ne_laisse_passer_que_la_progression():
    assert {nom for nom in EVENTS if is_terminal({"ev": nom})} == EVENTS - {"progress", "delta"}
    assert is_terminal(ev("progress", job_id="j1", pct=40)) is False
    # Une opération ne clôt rien : un worker qui renverrait l'`op` reçue ne doit
    # pas débloquer l'attente d'un résultat.
    assert is_terminal(op("infer", job_id="j1")) is False
