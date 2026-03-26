"""Synapse authentication helper.

Provides a single function to authenticate with Synapse using the
SYNAPSE_AUTH_TOKEN environment variable. This is stable boilerplate
used by the agent's dynamically generated scripts.
"""

from __future__ import annotations

import os
import synapseclient


def get_synapse_client() -> synapseclient.Synapse:
    """Return an authenticated Synapse client.

    Reads the auth token from the SYNAPSE_AUTH_TOKEN environment variable.
    Raises EnvironmentError if the variable is not set.
    Raises synapseclient.SynapseAuthenticationError on invalid token.
    """
    token = os.environ.get("SYNAPSE_AUTH_TOKEN")
    if not token:
        raise EnvironmentError(
            "SYNAPSE_AUTH_TOKEN environment variable is not set. "
            "This must be the nf-bot service account token."
        )
    syn = synapseclient.Synapse()
    syn.login(authToken=token, silent=True)
    return syn
