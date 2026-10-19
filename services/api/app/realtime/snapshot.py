"""Snapshot/delta protocol constants and helpers shared by the REST routers and the CORS config."""

from fastapi import Response

from app.realtime import streams

SEQ_HEADER = "X-Channel-Seq"
EPOCH_HEADER = "X-Channel-Epoch"


def set_headers(response: Response, head: streams.Head) -> None:
    response.headers[EPOCH_HEADER] = head.epoch
    response.headers[SEQ_HEADER] = str(head.seq)
