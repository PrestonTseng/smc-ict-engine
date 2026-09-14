# Adapter boundary

Provider, notifier, repository, lock, and scheduler implementations are outside the foundation card. Their interfaces live in `application/ports` and their IDs fail closed in `composition`.
