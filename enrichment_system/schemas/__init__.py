# evaluation_system/schemas/__init__.py

from .transactions import TransactionInput, EnrichedTransaction

# Limita ciò che viene importato se qualcuno fa `from schemas import *`
# e nasconde dettagli implementativi interni di transactions.py
__all__ = ["TransactionInput", "EnrichedTransaction"]