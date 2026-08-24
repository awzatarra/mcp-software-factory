import json
from collections.abc import Callable

from streaming.formatting import format_terminal_event
from streaming.models import WorkflowEvent


class TerminalWorkflowEventConsumer:
    def __init__(
        self,
        *,
        debug: bool = False,
        compact_sequence: bool = True,
        start_sequence: int = 0,
        output: Callable[[str], None] = print,
    ) -> None:
        self.debug = debug
        self.compact_sequence = compact_sequence
        self.output = output
        self.start_sequence = start_sequence
        self._display_sequences: dict[str, int] = {}

    def consume(self, event: WorkflowEvent) -> None:
        if self.debug:
            self.output(json.dumps(event.model_dump(mode="json"), ensure_ascii=False))
            return
        if self.compact_sequence:
            display_sequence = self._display_sequences.get(
                event.thread_id,
                self.start_sequence,
            ) + 1
            self._display_sequences[event.thread_id] = display_sequence
            display_event = event.model_copy(update={"sequence": display_sequence})
        else:
            display_event = event
        self.output(format_terminal_event(display_event))

    def __call__(self, event: WorkflowEvent) -> None:
        self.consume(event)
