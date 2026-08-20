"""Constat de validation — ce que le registre a à redire, sans lever d'exception.

Un manifeste invalide n'interrompt pas le chargement : tout est collecté, et
c'est l'appelant (CLI, CI, API) qui décide quoi en faire. Le type vit dans son
propre module pour que le chargeur de contrats de capacité et celui des
manifestes puissent tous deux l'utiliser sans se référencer l'un l'autre.
"""

from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    severity: Severity
    file: str
    message: str
