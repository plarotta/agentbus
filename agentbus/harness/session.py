import json
from pathlib import Path
from uuid import uuid4

from agentbus.schemas.harness import ConversationTurn

DEFAULT_SESSION_ROOT = Path.home() / ".agentbus" / "sessions"


class Session:
    """Plain-JSON conversation session persistence."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        turns: list[ConversationTurn] | None = None,
        root_dir: Path | str | None = None,
        file_path: Path | str | None = None,
    ) -> None:
        self.session_id = session_id or str(uuid4())
        self.turns = list(turns or [])
        self.root_dir = Path(root_dir) if root_dir is not None else DEFAULT_SESSION_ROOT
        self.dir_path = self.root_dir / self.session_id
        self.file_path = Path(file_path) if file_path is not None else self.dir_path / "main.json"

    def append(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)

    def total_tokens(self) -> int:
        """Sum of token_count across all turns."""
        return sum(t.token_count for t in self.turns)

    def save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.session_id,
            "file": self.file_path.name,
            "turns": [turn.model_dump(mode="json") for turn in self.turns],
        }
        self.file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(
        cls,
        session_id: str,
        *,
        root_dir: Path | str | None = None,
        file_name: str = "main.json",
    ) -> "Session":
        root = Path(root_dir) if root_dir is not None else DEFAULT_SESSION_ROOT
        file_path = root / session_id / file_name
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        turns = [ConversationTurn.model_validate(turn) for turn in payload.get("turns", [])]
        return cls(
            payload.get("session_id", session_id),
            turns=turns,
            root_dir=root,
            file_path=file_path,
        )

    def fork(self, from_turn_index: int) -> "Session":
        self.dir_path.mkdir(parents=True, exist_ok=True)
        existing_numbers = []
        for path in self.dir_path.glob("branch_*.json"):
            suffix = path.stem.removeprefix("branch_")
            if suffix.isdigit():
                existing_numbers.append(int(suffix))
        branch_number = max(existing_numbers, default=0) + 1
        branch_path = self.dir_path / f"branch_{branch_number}.json"
        forked_turns = self.turns[: from_turn_index + 1]
        branch = Session(
            self.session_id,
            turns=forked_turns,
            root_dir=self.root_dir,
            file_path=branch_path,
        )
        branch.save()
        return branch


__all__ = ["DEFAULT_SESSION_ROOT", "Session"]
