# Note-evidence fixtures

`corpus.json` indexes six curated/synthetic Persian and mixed-language transcript/
reference pairs. Tests check numbers, terms, tables, tips and unsupported claims
without provider calls. Keep each pair together; references must not add facts.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_note_evidence.py -q
```

```bash
python -m pytest tests/test_note_evidence.py -q
```

Activate the development virtualenv on cPanel/Linux first. Never add private lectures.
