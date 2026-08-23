"""Protocole worker — JSON Lines (CONCEPTION.md §5.1).

Une ligne JSON par message, dans les deux sens. Le superviseur envoie des
**opérations** (`op`), le worker répond par des **événements** (`ev`) :

    → {"op":"load","variant":{…}}
    ← {"ev":"loaded","warmup_ms":2400,"peak_memory_bytes":3100000000,"options":{…}}
    → {"op":"infer","job_id":"j1","input":{…},"params":{…},"output_dir":"…","seed":42}
    ← {"ev":"progress","job_id":"j1","pct":40,"note":"…"}     (0..n)
    ← {"ev":"delta","job_id":"j1","text":"…","channel":"answer"}   (0..n)
    ← {"ev":"result","job_id":"j1","output":{"audio":"audio.wav"},"metrics":{…}}
    ← {"ev":"error","job_id":"j1","message":"…","trace":"…"}
    → {"op":"unload"}  ← {"ev":"unloaded",…}
    → {"op":"ping"}    ← {"ev":"pong","rss_bytes":…}

Deux règles portent tout le reste :

- les sorties binaires vont **en fichiers** dans `output_dir`, jamais en base64
  sur le canal — un maillage de 200 Mo ne traverse pas un tube ;
- le canal ne transporte que du protocole. Ce module est en bibliothèque
  standard pure : il est importé des deux côtés, et le côté worker vit dans un
  venv isolé qui ne connaît rien d'Écurie.

`delta` porte le texte au fur et à mesure qu'il se produit. Il est **facultatif
et sans conséquence** : le `result` reste la seule source de vérité, et un worker
qui n'en émet aucun se comporte comme avant. Ce que le flux change n'est pas le
résultat mais l'attente — une réponse de deux minutes qui s'écrit sous les yeux
n'est pas la même chose qu'un curseur qui tourne pendant deux minutes. Son champ
`channel` sépare le brouillon de raisonnement de la réponse : les mêler serait
irréversible, personne ne pouvant les redécouper après coup dans un flux de
caractères.

`unloaded` n'est pas au tableau de la conception ; il est nécessaire. Sans accusé
de déchargement, le superviseur ne saurait pas quand la mémoire est rendue, et le
contrôle d'admission (§5.4) chargerait le modèle suivant par-dessus le précédent —
exactement le swap qu'il existe pour éviter.
"""

import json
from typing import Any

# Opérations (superviseur → worker)
OP_LOAD = "load"
OP_INFER = "infer"
OP_UNLOAD = "unload"
OP_PING = "ping"

# Événements (worker → superviseur)
EV_LOADED = "loaded"
EV_PROGRESS = "progress"
EV_DELTA = "delta"
EV_RESULT = "result"
EV_ERROR = "error"
EV_PONG = "pong"
EV_UNLOADED = "unloaded"

# Les deux canaux d'un `delta`. Un modèle qui raisonne à voix haute produit deux
# textes et non un seul, et les mêler serait irréversible : personne ne peut
# séparer après coup un brouillon d'une réponse dans un flux de caractères.
CANAL_REPONSE = "answer"
CANAL_RAISONNEMENT = "reasoning"
CANAUX = frozenset({CANAL_REPONSE, CANAL_RAISONNEMENT})

OPS = frozenset({OP_LOAD, OP_INFER, OP_UNLOAD, OP_PING})
EVENTS = frozenset(
    {EV_LOADED, EV_PROGRESS, EV_DELTA, EV_RESULT, EV_ERROR, EV_PONG, EV_UNLOADED}
)

# Un message qui dépasse cette taille est le signe d'une sortie binaire passée en
# ligne : on refuse plutôt que de faire gonfler la mémoire du superviseur.
MAX_LINE_BYTES = 1 << 20


class ProtocolError(RuntimeError):
    """Message illisible, incomplet, ou hors du vocabulaire du protocole."""


def op(name: str, **fields: Any) -> dict[str, Any]:
    if name not in OPS:
        raise ProtocolError(f"opération inconnue : {name!r}")
    return {"op": name, **fields}


def ev(name: str, **fields: Any) -> dict[str, Any]:
    if name not in EVENTS:
        raise ProtocolError(f"événement inconnu : {name!r}")
    return {"ev": name, **fields}


def encode(message: dict[str, Any]) -> bytes:
    """Sérialise un message en une ligne. Refuse ce qui n'est pas un message.

    Le *nom* n'est pas revalidé ici, alors que `op()` et `ev()` le font : la
    réception doit tolérer un nom qu'elle ne connaît pas — un worker plus récent
    a le droit d'émettre un événement que ce superviseur ignore, et le rejeter
    au décodage rendrait toute évolution du protocole impossible. Le garde-fou
    est à la construction, là où c'est notre code qui parle.
    """
    kind = kind_of(message)
    if kind is None:
        raise ProtocolError(_pourquoi_pas_un_message(message))
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_LINE_BYTES:
        raise ProtocolError(
            f"message de {len(payload)} octets : les sorties volumineuses passent "
            "par des fichiers dans output_dir, jamais par le canal"
        )
    return payload + b"\n"


def decode(line: bytes | str) -> dict[str, Any]:
    """Analyse une ligne reçue. Lève `ProtocolError` sur tout ce qui n'est pas un message."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"ligne non-UTF-8 : {exc}") from exc
    line = line.strip()
    if not line:
        raise ProtocolError("ligne vide")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"JSON illisible : {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(f"message qui n'est pas un objet : {type(message).__name__}")
    if kind_of(message) is None:
        raise ProtocolError(_pourquoi_pas_un_message(message))
    return message


def _pourquoi_pas_un_message(message: dict[str, Any]) -> str:
    """Distingue les deux façons de n'être pas un message.

    « sans clé op ni ev » sur un document qui porte les deux envoie chercher une
    clé manquante qui, elle, est là deux fois — le diagnostic part dans la
    mauvaise direction, et le seul indice est la liste des clés en fin de ligne.
    """
    if "op" in message and "ev" in message:
        return (
            f"message portant à la fois 'op' ({message['op']!r}) et 'ev' "
            f"({message['ev']!r}) : on ne devine pas le sens de circulation"
        )
    return f"message sans clé 'op' ni 'ev' : {sorted(message)}"


def kind_of(message: dict[str, Any]) -> str | None:
    """« op », « ev », ou None. Les deux clés à la fois, c'est None : on ne devine pas."""
    has_op, has_ev = "op" in message, "ev" in message
    if has_op == has_ev:
        return None
    return "op" if has_op else "ev"


def name_of(message: dict[str, Any]) -> str:
    kind = kind_of(message)
    if kind is None:
        raise ProtocolError(_pourquoi_pas_un_message(message))
    name = message[kind]
    if not isinstance(name, str):
        raise ProtocolError(f"nom de message qui n'est pas une chaîne : {name!r}")
    return name


# Ce qui n'arrête pas une opération. La liste est écrite en positif, et c'est
# délibéré : `is_terminal` traitait auparavant « tout sauf progress » comme une
# fin, si bien que le premier `delta` d'un modèle aurait clos son propre job —
# un flux qui coupe la génération qu'il rapporte.
EVENTS_EN_COURS = frozenset({EV_PROGRESS, EV_DELTA})


def is_terminal(message: dict[str, Any]) -> bool:
    """Un événement qui clôt une opération — tout le reste est de la progression."""
    return kind_of(message) == "ev" and name_of(message) not in EVENTS_EN_COURS
