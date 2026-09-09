"""W85: a command thread older than CMD_MAX_SEC no longer blocks the command poller."""
from webscraper import agent


def test_cmd_stuck_rule():
    assert agent._cmd_stuck(100.0, 100.0 + agent.CMD_MAX_SEC + 1, True) is True
    assert agent._cmd_stuck(100.0, 100.0 + agent.CMD_MAX_SEC - 1, True) is False
    assert agent._cmd_stuck(100.0, 100.0 + agent.CMD_MAX_SEC + 1, False) is False
    assert agent._cmd_stuck(0.0, 10_000.0, True) is False
