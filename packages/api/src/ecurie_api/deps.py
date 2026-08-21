"""Accès au contexte partagé depuis une route."""

from typing import Annotated

from fastapi import Depends, Request

from ecurie_api.state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.ecurie


StateDep = Annotated[AppState, Depends(get_state)]
