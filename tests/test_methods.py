import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from edgelab.methods import BY_ID, DIMENSION, NAMESPACE, QUERY, REQUIRED_METHOD_IDS, PineconeMethods
from edgelab.planning import search
from edgelab.providers import ProviderError


HOST = "methods-000.svc.us-east1.pinecone.io"


def exact_reply():
    return {"namespace": NAMESPACE, "vectors": {
        note_id: {"id": note_id, "metadata": BY_ID[note_id].metadata()}
        for note_id in reversed(REQUIRED_METHOD_IDS)
    }}


class RequiredMethodTests(unittest.TestCase):
    def test_planning_fetches_verified_ids_without_semantic_ranking(self):
        get = Mock(return_value=exact_reply())
        post = Mock()
        provider = PineconeMethods(HOST, "test-key", transport=post, get_transport=get)

        notes = provider.retrieve("cointegration")

        self.assertEqual(tuple(note.id for note in notes), REQUIRED_METHOD_IDS)
        self.assertEqual(search(notes)["status"], "awaiting_approval")
        post.assert_not_called()
        get.assert_called_once()
        url, headers, timeout = get.call_args.args
        parsed = urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", HOST, "/vectors/fetch"))
        self.assertEqual(parse_qs(parsed.query), {"ids": list(REQUIRED_METHOD_IDS), "namespace": [NAMESPACE]})
        self.assertEqual(headers, {"Api-Key": "test-key", "X-Pinecone-Api-Version": "2025-10"})
        self.assertEqual(timeout, 15)

    def test_missing_or_mismatched_record_cannot_be_supplied_by_ranking(self):
        for mismatch in (None, {"id": "kpss"}, {"topic": "retrieval"}, {"corpus_version": "older"},
                         {"content_sha256": "0" * 64}, {"public": 1}, {"reviewed": False},
                         {"active": False}, {"text": "Wrong text"}):
            with self.subTest(mismatch=mismatch):
                reply = exact_reply()
                if mismatch is None:
                    del reply["vectors"]["adf"]
                else:
                    reply["vectors"]["adf"]["metadata"].update(mismatch)
                post = Mock()
                provider = PineconeMethods(HOST, "test-key", transport=post, get_transport=Mock(return_value=reply))

                notes = provider.retrieve("cointegration")

                self.assertEqual(tuple(note.id for note in notes), ("kpss", "eg"))
                self.assertEqual(search(notes)["status"], "unresolved")
                post.assert_not_called()

    def test_invalid_namespace_or_response_scope_fails_closed(self):
        reply = exact_reply()
        for invalid in ([], {**reply, "namespace": "other"}, {**reply, "vectors": []},
                        {**reply, "vectors": {**reply["vectors"], "extra": {}}}):
            with self.subTest(invalid=invalid):
                post = Mock()
                provider = PineconeMethods(HOST, "test-key", transport=post, get_transport=Mock(return_value=invalid))
                with self.assertRaises(ProviderError):
                    provider.required_notes()
                post.assert_not_called()

    def test_diagnostic_reports_semantic_recall_separately(self):
        kpss = BY_ID["kpss"]
        get = Mock(return_value=exact_reply())
        post = Mock(side_effect=[
            {"data": [{"values": [0.1] * DIMENSION}]},
            {"namespace": NAMESPACE, "matches": [{"id": kpss.id, "score": 0.9, "metadata": kpss.metadata()}]},
            {"data": [{"index": 0, "score": 0.8}]},
        ])
        provider = PineconeMethods(HOST, "test-key", transport=post, get_transport=get)

        diagnostic = provider.retrieval_diagnostic()

        self.assertEqual(diagnostic["required"]["status"], "complete")
        self.assertEqual(diagnostic["required"]["verified_ids"], list(REQUIRED_METHOD_IDS))
        self.assertEqual(diagnostic["required"]["missing_ids"], [])
        self.assertEqual(diagnostic["semantic"]["note_ids"], ["kpss"])
        self.assertEqual(diagnostic["semantic"]["recall_at_3"], 1 / 3)
        self.assertEqual(post.call_args_list[0].args[2]["inputs"], [{"text": QUERY["cointegration"]}])
        self.assertEqual(post.call_args_list[-1].args[2]["query"], QUERY["cointegration"])
        self.assertNotIn("test-key", json.dumps(diagnostic))

    def test_diagnostic_semantic_failure_preserves_verified_required_ids(self):
        get = Mock(return_value=exact_reply())
        post = Mock(side_effect=ProviderError("private error details"))
        provider = PineconeMethods(HOST, "test-key", transport=post, get_transport=get)

        diagnostic = provider.retrieval_diagnostic()

        self.assertEqual(diagnostic["required"]["verified_ids"], list(REQUIRED_METHOD_IDS))
        self.assertEqual(diagnostic["semantic"]["status"], "unavailable")
        self.assertIsNone(diagnostic["semantic"]["recall_at_3"])
        self.assertNotIn("private error details", json.dumps(diagnostic))
