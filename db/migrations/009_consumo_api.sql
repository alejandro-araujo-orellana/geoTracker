-- 009 — Contador de consumo de APIs externas (pool de peticiones, B.2).
--
-- Una fila por día (hora local de SLP) y API. src/presupuesto.py lee el
-- consumo del mes para calcular cuántas peticiones puede gastar cada ciclo
-- y suma cada intento HTTP (incluidos reintentos) al terminar el ciclo.
-- bloqueado = se recibió un 403 ese día (cuota agotada o key inválida):
-- no se vuelve a llamar hasta el día siguiente.

CREATE TABLE IF NOT EXISTS consumo_api (
    fecha      date    NOT NULL,
    api        text    NOT NULL,
    peticiones integer NOT NULL DEFAULT 0 CHECK (peticiones >= 0),
    bloqueado  boolean NOT NULL DEFAULT FALSE,
    PRIMARY KEY (fecha, api)
);
