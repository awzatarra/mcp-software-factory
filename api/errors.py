class ApiConflictError(RuntimeError):
    pass


class ApprovalConflictError(ApiConflictError):
    pass
