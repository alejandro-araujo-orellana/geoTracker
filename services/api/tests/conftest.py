"""Variables mínimas para importar la app en los tests (antes de que se importe app.main)."""

import os

for _var, _valor in (
    ("DB_USER", "test"), ("DB_PASSWORD", "test"), ("DB_NAME", "test"),
    # Token de prueba (D26). Si el entorno ya trae API_TOKENS (p. ej. --env-file .env), se usa ese.
    ("API_TOKENS", "token-de-prueba-0123456789abcdef0123456789abcdef"),
):
    os.environ.setdefault(_var, _valor)
