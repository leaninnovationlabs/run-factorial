"""The `secret` table, as the rest of the orchestrator sees it.

Postgres is the only secret store the orchestrator manages (design.md §7.1). Values go in as
ciphertext and come back as `SecretStr`; plaintext is never stored, logged, or put in an event.

A manifest's `secrets:` block says what to mint; it is not an isolation boundary (design.md
§7.4). A substrate may inject every stored secret into every run regardless of what the agent
declared, so `resolve_for_run` narrowing to the declared keys is an adapter convenience. The
real limits are what the store holds and what each token is scoped to.

`runtime_credential` is the same store keyed by a registry `runtime` row instead of a project;
both tables share one implementation, and the `*_for_runtime` twins below differ only in the
owner column and in the AAD namespace. `organization_credential` holds an organization's own,
such as the assistant's; its `*_for_organization` functions stand alone on the crypto.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from pydantic import SecretStr
from sqlalchemy import ColumnElement, select
from sqlalchemy import delete as sql_delete
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from orchestrator.db.models import OrganizationCredential, RuntimeCredential, Secret
from orchestrator.secrets.crypto import EncryptedValue, decrypt, encrypt

#: A stored name becomes an environment variable in a run, so it must be shaped like one.
NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

__all__ = [
    "NAME_RE",
    "Delivery",
    "delete",
    "delete_for_organization",
    "delete_for_runtime",
    "deliveries",
    "get",
    "get_for_organization",
    "get_for_runtime",
    "get_many",
    "list_names",
    "list_names_for_runtime",
    "organization_credential_meta",
    "organization_credentials_meta",
    "put",
    "put_for_organization",
    "put_for_runtime",
    "resolve_for_run",
    "resolve_for_runtime",
    "set_delivery",
    "verify_readable",
]


def _aad(project_id: int, name: str) -> bytes:
    """Bind ciphertext to its row: a blob copied to another project or renamed then fails to
    authenticate rather than decrypting into a context it was never granted for."""
    return f"{project_id}:{name}".encode()


def _runtime_aad(runtime_id: int, name: str) -> bytes:
    """Namespaced, unlike `_aad`: project and runtime ids are separate identity sequences, so
    equal integers are routine and must never authenticate each other's blobs."""
    return f"runtime:{runtime_id}:{name}".encode()


def _organization_aad(organization_id: int, name: str) -> bytes:
    """Namespaced like `_runtime_aad`, and bound to the organization: a blob copied to another
    organization, or from the project or runtime store, never authenticates here."""
    return f"org:{organization_id}:{name}".encode()


_Model = type[Secret] | type[RuntimeCredential]


@dataclass(frozen=True, slots=True)
class Delivery:
    """How a run is supplied one project secret (tech-stack decision 75): the agent's
    environment, or a header a cloud sandbox's egress proxy sets on `host`."""

    mode: Literal["header", "env"]
    host: str | None = None
    header: str | None = None


def _delivery_columns(delivery: Delivery) -> dict[str, str | None]:
    return {
        "sandbox_delivery": delivery.mode,
        "egress_host": delivery.host,
        "egress_header": delivery.header,
    }


async def _put(
    session: AsyncSession,
    model: _Model,
    owner_col: InstrumentedAttribute[int],
    owner_id: int,
    name: str,
    plaintext: str,
    aad: bytes,
    columns: Mapping[str, object] | None = None,
) -> None:
    value: EncryptedValue = encrypt(plaintext, aad=aad)
    columns = columns or {}

    row = (
        await session.execute(select(model).where(owner_col == owner_id, model.name == name))
    ).scalar_one_or_none()

    if row is None:
        session.add(
            model(
                **{
                    owner_col.key: owner_id,
                    "name": name,
                    "value_ciphertext": value.ciphertext,
                    "enc_key_id": value.enc_key_id,
                    **columns,
                }
            )
        )
    else:
        row.value_ciphertext = value.ciphertext
        row.enc_key_id = value.enc_key_id
        for column, column_value in columns.items():
            setattr(row, column, column_value)
    await session.flush()


async def _get(
    session: AsyncSession,
    model: _Model,
    owner_col: InstrumentedAttribute[int],
    owner_id: int,
    name: str,
    aad: bytes,
) -> SecretStr | None:
    row = (
        await session.execute(
            select(model.value_ciphertext, model.enc_key_id).where(
                owner_col == owner_id, model.name == name
            )
        )
    ).first()
    if row is None:
        return None
    return decrypt(EncryptedValue(ciphertext=bytes(row[0]), enc_key_id=str(row[1])), aad=aad)


async def _delete(
    session: AsyncSession,
    model: _Model,
    owner_col: InstrumentedAttribute[int],
    owner_id: int,
    name: str,
) -> bool:
    result = await session.execute(
        sql_delete(model).where(owner_col == owner_id, model.name == name)
    )
    await session.flush()
    # `execute` is typed as the generic Result; a DELETE's is the CursorResult with rowcount.
    assert isinstance(result, CursorResult)
    return bool(result.rowcount)


async def _names(
    session: AsyncSession, model: _Model, owner_col: InstrumentedAttribute[int], owner_id: int
) -> Sequence[str]:
    rows = (
        await session.execute(select(model.name).where(owner_col == owner_id).order_by(model.name))
    ).all()
    return [str(row[0]) for row in rows]


async def put(
    session: AsyncSession,
    project_id: int,
    name: str,
    plaintext: str,
    delivery: Delivery | None = None,
) -> None:
    """Insert or replace one secret. Re-encrypts under the current master key every time, so an
    overwrite is also the rotation path for that row. The row is replaced whole: a rewrite
    without `delivery` is an environment variable again."""
    await _put(
        session,
        Secret,
        Secret.project_id,
        project_id,
        name,
        plaintext,
        _aad(project_id, name),
        _delivery_columns(delivery or Delivery(mode="env")),
    )


async def set_delivery(
    session: AsyncSession, project_id: int, name: str, delivery: Delivery
) -> bool:
    """Change how a stored secret is supplied, leaving its value as it is. False when the project
    has no secret by that name."""
    result = await session.execute(
        sql_update(Secret)
        .where(Secret.project_id == project_id, Secret.name == name)
        .values(**_delivery_columns(delivery))
    )
    await session.flush()
    assert isinstance(result, CursorResult)
    return bool(result.rowcount)


async def deliveries(session: AsyncSession, project_id: int) -> dict[str, Delivery]:
    """How each project secret is supplied, by name. Not secret, so the page shows them."""
    rows = await session.execute(
        select(Secret.name, Secret.sandbox_delivery, Secret.egress_host, Secret.egress_header)
        .where(Secret.project_id == project_id)
        .order_by(Secret.name)
    )
    return {
        name: Delivery(mode=mode, host=host, header=header)
        for name, mode, host, header in rows.all()
    }


async def get(session: AsyncSession, project_id: int, name: str) -> SecretStr | None:
    """One decrypted value, or `None` if the project has no secret by that name."""
    return await _get(session, Secret, Secret.project_id, project_id, name, _aad(project_id, name))


async def get_many(
    session: AsyncSession, project_id: int, names: Collection[str]
) -> dict[str, SecretStr]:
    """The decrypted values of those of `names` the project holds, in one query: a name it does
    not hold is absent, and nothing outside `names` is decrypted."""
    if not names:
        return {}
    rows = await session.execute(
        select(Secret.name, Secret.value_ciphertext, Secret.enc_key_id).where(
            Secret.project_id == project_id, Secret.name.in_(names)
        )
    )
    return {
        str(name): decrypt(
            EncryptedValue(ciphertext=bytes(ciphertext), enc_key_id=str(key_id)),
            aad=_aad(project_id, str(name)),
        )
        for name, ciphertext, key_id in rows.all()
    }


async def delete(session: AsyncSession, project_id: int, name: str) -> bool:
    """Remove one project secret. False when there was nothing to remove."""
    return await _delete(session, Secret, Secret.project_id, project_id, name)


async def list_names(session: AsyncSession, project_id: int) -> Sequence[str]:
    """Names only — all the dashboard and admin API may see. There is no "list values" call."""
    return await _names(session, Secret, Secret.project_id, project_id)


async def resolve_for_run(
    session: AsyncSession, project_id: int, keys: Sequence[str]
) -> dict[str, SecretStr]:
    """Materialise the secrets an agent run declared, at spawn time.

    Missing keys are simply absent from the result; the caller decides whether that is fatal,
    and the adapter reports the manifest/store mismatch with better context than this layer has.
    """
    resolved: dict[str, SecretStr] = {}
    for key in keys:
        value = await get(session, project_id, key)
        if value is not None:
            resolved[key] = value
    return resolved


async def put_for_runtime(
    session: AsyncSession, runtime_id: int, name: str, plaintext: str
) -> None:
    """Insert or replace one runtime credential; an overwrite is also that row's rotation."""
    await _put(
        session,
        RuntimeCredential,
        RuntimeCredential.runtime_id,
        runtime_id,
        name,
        plaintext,
        _runtime_aad(runtime_id, name),
    )


async def get_for_runtime(session: AsyncSession, runtime_id: int, name: str) -> SecretStr | None:
    """One decrypted value, or `None` if the runtime has no credential by that name."""
    return await _get(
        session,
        RuntimeCredential,
        RuntimeCredential.runtime_id,
        runtime_id,
        name,
        _runtime_aad(runtime_id, name),
    )


async def list_names_for_runtime(session: AsyncSession, runtime_id: int) -> Sequence[str]:
    """Names only, like `list_names` — the most the API and the dashboard may see."""
    return await _names(session, RuntimeCredential, RuntimeCredential.runtime_id, runtime_id)


async def delete_for_runtime(session: AsyncSession, runtime_id: int, name: str) -> bool:
    """Remove one credential. False when there was nothing to remove."""
    return await _delete(session, RuntimeCredential, RuntimeCredential.runtime_id, runtime_id, name)


async def resolve_for_runtime(session: AsyncSession, runtime_id: int) -> dict[str, SecretStr]:
    """Every credential the runtime holds, at spawn time — the one sanctioned unwrap, like
    `resolve_for_run`. Nothing narrows it: a runtime's credentials are all shaped for its
    adapter, which is why they live on the runtime and not the project."""
    return {
        name: value
        for name in await list_names_for_runtime(session, runtime_id)
        if (value := await get_for_runtime(session, runtime_id, name)) is not None
    }


async def put_for_organization(
    session: AsyncSession, organization_id: int, name: str, plaintext: str, *, by: str
) -> None:
    """Insert or replace one organization credential. Nothing commits here, as with `_put`."""
    value = encrypt(plaintext, aad=_organization_aad(organization_id, name))
    insert = pg_insert(OrganizationCredential).values(
        organization_id=organization_id,
        name=name,
        value_ciphertext=value.ciphertext,
        enc_key_id=value.enc_key_id,
        updated_by=by,
    )
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[OrganizationCredential.organization_id, OrganizationCredential.name],
            set_={
                "value_ciphertext": insert.excluded.value_ciphertext,
                "enc_key_id": insert.excluded.enc_key_id,
                "updated_by": insert.excluded.updated_by,
            },
        )
    )
    await session.flush()


def _organization_row(organization_id: int, name: str) -> ColumnElement[bool]:
    return (OrganizationCredential.organization_id == organization_id) & (
        OrganizationCredential.name == name
    )


async def get_for_organization(
    session: AsyncSession, organization_id: int, name: str
) -> SecretStr | None:
    row = (
        await session.execute(
            select(
                OrganizationCredential.value_ciphertext, OrganizationCredential.enc_key_id
            ).where(_organization_row(organization_id, name))
        )
    ).first()
    if row is None:
        return None
    return decrypt(
        EncryptedValue(ciphertext=bytes(row[0]), enc_key_id=str(row[1])),
        aad=_organization_aad(organization_id, name),
    )


async def delete_for_organization(session: AsyncSession, organization_id: int, name: str) -> bool:
    result = await session.execute(
        sql_delete(OrganizationCredential).where(_organization_row(organization_id, name))
    )
    await session.flush()
    assert isinstance(result, CursorResult)
    return bool(result.rowcount)


async def organization_credential_meta(
    session: AsyncSession, organization_id: int, name: str
) -> tuple[str, datetime] | None:
    """Who last set it and when: all a person may learn about a stored credential."""
    row = (
        await session.execute(
            select(OrganizationCredential.updated_by, OrganizationCredential.updated_at).where(
                _organization_row(organization_id, name)
            )
        )
    ).first()
    return None if row is None else (str(row[0]), row[1])



async def organization_credentials_meta(
    session: AsyncSession, organization_id: int, prefix: str
) -> list[tuple[str, str, datetime]]:
    """`(name, updated_by, updated_at)` of every credential whose name starts with `prefix`."""
    rows = await session.execute(
        select(
            OrganizationCredential.name,
            OrganizationCredential.updated_by,
            OrganizationCredential.updated_at,
        )
        .where(
            OrganizationCredential.organization_id == organization_id,
            OrganizationCredential.name.startswith(prefix, autoescape=True),
        )
        .order_by(OrganizationCredential.name)
    )
    return [(str(name), str(by), at) for name, by, at in rows]

async def verify_readable(session: AsyncSession) -> None:
    """Decrypt one stored row from each table of ciphertext, so a master key that cannot read
    what the database holds fails here and not on the first run that needs a secret. Raises
    `DecryptionError` for the first row that does not decrypt; an empty table passes."""
    row = (
        await session.execute(
            select(Secret.project_id, Secret.name, Secret.value_ciphertext, Secret.enc_key_id)
            .order_by(Secret.id)
            .limit(1)
        )
    ).first()
    if row is not None:
        decrypt(
            EncryptedValue(ciphertext=bytes(row[2]), enc_key_id=str(row[3])),
            aad=_aad(row[0], str(row[1])),
        )
    row = (
        await session.execute(
            select(
                RuntimeCredential.runtime_id,
                RuntimeCredential.name,
                RuntimeCredential.value_ciphertext,
                RuntimeCredential.enc_key_id,
            )
            .order_by(RuntimeCredential.id)
            .limit(1)
        )
    ).first()
    if row is not None:
        decrypt(
            EncryptedValue(ciphertext=bytes(row[2]), enc_key_id=str(row[3])),
            aad=_runtime_aad(row[0], str(row[1])),
        )
    row = (
        await session.execute(
            select(
                OrganizationCredential.organization_id,
                OrganizationCredential.name,
                OrganizationCredential.value_ciphertext,
                OrganizationCredential.enc_key_id,
            )
            .order_by(OrganizationCredential.id)
            .limit(1)
        )
    ).first()
    if row is not None:
        decrypt(
            EncryptedValue(ciphertext=bytes(row[2]), enc_key_id=str(row[3])),
            aad=_organization_aad(row[0], str(row[1])),
        )
