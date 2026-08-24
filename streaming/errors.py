class WorkflowEventStoreError(RuntimeError):
    pass


class WorkflowEventPersistenceError(WorkflowEventStoreError):
    pass


class ConflictingTerminalEventError(WorkflowEventStoreError):
    pass


class WorkflowEventStoreClosedError(WorkflowEventStoreError):
    pass
