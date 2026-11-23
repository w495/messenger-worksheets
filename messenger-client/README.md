# Messenger client dependency

The production deployment requires an authorized Messenger client that exports
`client.py` and `transport.py` in this directory.

Those modules are environment-specific and intentionally excluded from this
public repository. Obtain them through an authorized distribution channel. Do
not commit OAuth tokens, generated environment files, or transport credentials.

The client contract used by the gateway includes `set_reaction()` with
`SetReaction` registered as a confirmation-preview mutation, in addition to
message, document, image, history, thread, and media operations.
