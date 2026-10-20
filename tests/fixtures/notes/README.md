# Note-evidence test corpus

`corpus.json` indexes six curated Persian/mixed-language transcripts and their expected note structures. `tests/test_note_evidence.py` uses them to check numeric/term coverage, unsupported claims, table cells, practical-tip evidence and truncation behavior. The tests are deterministic and make no provider calls.

Run the tests from the repository root:

```powershell
python -m pytest tests/test_note_evidence.py -q
```

Keep each transcript and its matching reference together. When changing a fixture, preserve every source number and term; references must not add facts that the lecture does not contain. The files are test data, not real user recordings.
