from agentbus.harness.session import Session
from agentbus.schemas.harness import ConversationTurn


def test_session_create_persist_and_load(tmp_path):
    session = Session("session-1", root_dir=tmp_path)
    session.append(ConversationTurn(role="user", content="hello", token_count=1))
    session.append(ConversationTurn(role="assistant", content="world", token_count=1))
    session.save()

    loaded = Session.load("session-1", root_dir=tmp_path)

    assert loaded.session_id == session.session_id
    assert loaded.turns == session.turns


def test_session_total_tokens():
    session = Session("tok-1")
    session.append(ConversationTurn(role="user", content="hi", token_count=3))
    session.append(ConversationTurn(role="assistant", content="hello", token_count=5))
    assert session.total_tokens() == 8


def test_session_fork_creates_branch_without_mutating_main(tmp_path):
    session = Session("session-2", root_dir=tmp_path)
    for value in ("one", "two", "three", "four"):
        session.append(ConversationTurn(role="user", content=value, token_count=1))
    session.save()

    branch = session.fork(2)
    loaded_main = Session.load("session-2", root_dir=tmp_path)

    assert branch.file_path.name == "branch_1.json"
    assert branch.file_path.exists()
    assert [turn.content for turn in branch.turns] == ["one", "two", "three"]
    assert [turn.content for turn in loaded_main.turns] == ["one", "two", "three", "four"]
