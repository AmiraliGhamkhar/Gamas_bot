# STT fixtures

No audio is committed. `tests/test_stt.py` uses scripted HTTP responses to check
formats, language mapping, retries and failover without keys or network calls.
A passing mocked test does not verify a live provider or production audio quality.

Keep manual samples private and out of Git unless synthetic or explicitly cleared
for redistribution. Never commit identifiable student, medical or lecture data.
See [Testing](../../../docs/TESTING.md) for PowerShell and cPanel/Linux commands.
