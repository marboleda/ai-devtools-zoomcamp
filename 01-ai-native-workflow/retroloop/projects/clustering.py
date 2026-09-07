"""#14: AI clustering suggestion.

Immediately after a facilitator's reveal action (#12) succeeds, the revealed
cards are sent to the Anthropic API (tool-use, per stack.md) and the
response is written as ``Cluster`` rows with ``origin="suggested"``, each
card assigned to a cluster or left unclustered.

Clustering is a "starting arrangement" (stack.md invariant #2), not a
required step: any failure or timeout talking to the API is caught here and
simply results in zero suggested clusters — every card stays unclustered —
rather than blocking the reveal that already happened. There is no re-run
path: reveal itself can only happen once per cycle (#12's own state check),
so this function is only ever called once per cycle too.
"""
import logging

import anthropic

from .models import Card, Cluster

logger = logging.getLogger(__name__)

CLUSTERING_MODEL = "claude-sonnet-5"

SUGGEST_CLUSTERS_TOOL = {
    "name": "suggest_clusters",
    "description": (
        "Group the given retrospective feedback cards into a small number "
        "of thematic clusters, each with a short descriptive name. A card "
        "that doesn't clearly fit any theme should be left out of every "
        "cluster's card_ids list rather than forced into one."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "description": "The proposed thematic clusters.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "A short (2-5 word) name for the theme this "
                                "cluster captures."
                            ),
                        },
                        "card_ids": {
                            "type": "array",
                            "description": "IDs of the cards belonging to this cluster.",
                            "items": {"type": "integer"},
                        },
                    },
                    "required": ["name", "card_ids"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["clusters"],
        "additionalProperties": False,
    },
}


def _format_cards_for_prompt(cards):
    return "\n".join(f"- id={card.pk} [{card.category}] {card.text}" for card in cards)


def suggest_clusters_for_cycle(cycle):
    """Ask the Anthropic API to propose thematic clusters for every card in
    ``cycle`` and write the response as ``Cluster`` rows
    (``origin=Cluster.Origin.SUGGESTED``), assigning each mentioned card's
    ``cluster`` field.

    Any exception raised talking to the API (connection error, timeout, a
    malformed/unexpected response, ...) is caught here; on failure this
    function simply returns having created no clusters, per #14's decision
    that clustering never blocks the reveal that already happened.
    """
    cards = list(cycle.cards.all())
    if not cards:
        return
    cards_by_id = {card.pk: card for card in cards}

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=CLUSTERING_MODEL,
            max_tokens=4096,
            tools=[SUGGEST_CLUSTERS_TOOL],
            tool_choice={"type": "tool", "name": "suggest_clusters"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Group these retrospective feedback cards into a "
                        "small number of thematic clusters. Each card is "
                        "listed as 'id=<id> [<category>] <text>'.\n\n"
                        f"{_format_cards_for_prompt(cards)}"
                    ),
                }
            ],
        )
        tool_use_block = next(
            block for block in response.content if block.type == "tool_use"
        )
        clusters_data = tool_use_block.input["clusters"]
    except Exception:
        # Broad on purpose: any Anthropic SDK error, timeout, or unexpected
        # response shape all fall back to "zero suggested clusters" rather
        # than propagating into reveal_cycle.
        logger.exception("AI clustering suggestion failed for cycle %s", cycle.pk)
        return

    for position, cluster_data in enumerate(clusters_data):
        name = (cluster_data.get("name") or "").strip()
        card_ids = cluster_data.get("card_ids") or []
        matched_card_ids = [cid for cid in card_ids if cid in cards_by_id]
        if not name or not matched_card_ids:
            # A cluster with no name or no cards actually in this cycle
            # (a hallucinated ID, say) contributes nothing rather than
            # creating an empty/bogus Cluster row.
            continue
        cluster = Cluster.objects.create(
            cycle=cycle,
            name=name,
            origin=Cluster.Origin.SUGGESTED,
            position=position,
        )
        Card.objects.filter(pk__in=matched_card_ids).update(cluster=cluster)
