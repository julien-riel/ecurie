"""Scanners par gestionnaire. Chacun retourne des LocationRecord ; aucun ne lit
le contenu des fichiers (hachage niveaux 1–2 seulement, voir CONCEPTION.md §1.2)."""

from ecurie_store.scanners.common import announce, stat_record, symlink_record
from ecurie_store.scanners.fswalk import scan_tree
from ecurie_store.scanners.hf import scan_hf
from ecurie_store.scanners.ollama import scan_ollama

__all__ = ["announce", "scan_hf", "scan_ollama", "scan_tree", "stat_record", "symlink_record"]
