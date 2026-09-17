# Audited 3x-ui contract fixtures

These immutable upstream references define the 3.7/3.8 compatibility contracts used
by `tests/test_3xui_compat.py` and the feature artifacts under
`specs/001-3xui-37-38-compat/`:

| Release | Audited commit |
| --- | --- |
| `v3.7.0` | `f727d04f6522bb94a8fb52e8352fdcafb51c11e1` |
| `v3.8.0` | `837addf66e945a80080273b5d2a315dea765d748` |

Source repository: `https://github.com/MHSanaei/3x-ui`.

The tests intentionally encode only credential-free request/response shapes derived
from those commits. Real-panel persistence evidence remains a separate controlled
acceptance step; no production database, token, password, or subscription identifier
belongs in this directory.
