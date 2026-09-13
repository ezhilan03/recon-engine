import unittest
from langgraph.types import Command
from tests.test_human_approval import build_test_graph, fake_state


class ApprovalTests(unittest.TestCase):
    def test_low_confidence_cannot_disable_review_flag(self):
        state = fake_state()
        state["exceptions"] = state["exceptions"][:1]
        state["exceptions"][0]["evidence"]["resolution_proposal"]["confidence"] = 0.1
        app = build_test_graph()
        result = app.invoke(state, config={"configurable": {"thread_id": "low"}})
        self.assertEqual(result["__interrupt__"][0].value["flagged_reason"], "low_confidence")

    def test_invalid_human_reply_is_rejected(self):
        app = build_test_graph()
        config = {"configurable": {"thread_id": "invalid"}}
        app.invoke(fake_state(), config=config)
        with self.assertRaises(ValueError):
            app.invoke(Command(resume="maybe"), config=config)
