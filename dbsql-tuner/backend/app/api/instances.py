"""Oracle instance management — CRUD + one-shot connection test."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import OracleConnectionError, OraclePermissionError
from ..core.security import decrypt_password, encrypt_password
from ..db.session import get_db
from ..models import OracleInstance
from ..oracle.connection import OracleConnectionManager
from ..schemas import (
    OracleConnectionTestResult,
    OracleInstanceCreate,
    OracleInstanceOut,
    OracleInstanceUpdate,
)

router = APIRouter(prefix="/api/v1/instances", tags=["oracle-instances"])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[OracleInstanceOut])
async def list_instances(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OracleInstance).order_by(OracleInstance.id))
    return result.scalars().all()


@router.get("/{instance_id}", response_model=OracleInstanceOut)
async def get_instance(instance_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return row


@router.post("", response_model=OracleInstanceOut, status_code=status.HTTP_201_CREATED)
async def create_instance(payload: OracleInstanceCreate, db: AsyncSession = Depends(get_db)):
    row = OracleInstance(
        name=payload.name,
        host=payload.host,
        port=payload.port,
        service_name=payload.service_name,
        read_user=payload.read_user,
        read_password=encrypt_password(payload.read_password),
        sandbox_user=payload.sandbox_user,
        sandbox_password=(
            encrypt_password(payload.sandbox_password) if payload.sandbox_password else None
        ),
        status="unknown",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(f"Created Oracle instance {row.id}: {row.name}")
    return row


@router.patch("/{instance_id}", response_model=OracleInstanceOut)
async def update_instance(
    instance_id: int, payload: OracleInstanceUpdate, db: AsyncSession = Depends(get_db)
):
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")

    update_data = payload.model_dump(exclude_unset=True)
    # Encrypt password fields if supplied.
    if "read_password" in update_data:
        update_data["read_password"] = encrypt_password(update_data["read_password"])
    if "sandbox_password" in update_data and update_data["sandbox_password"]:
        update_data["sandbox_password"] = encrypt_password(update_data["sandbox_password"])

    for key, value in update_data.items():
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)
    # Drop cached pool so next use re-connects with new credentials.
    OracleConnectionManager.get().close_pool(instance_id)
    logger.info(f"Updated Oracle instance {instance_id}")
    return row


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_instance(instance_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    OracleConnectionManager.get().close_pool(instance_id)
    await db.delete(row)
    await db.commit()
    logger.info(f"Deleted Oracle instance {instance_id}")


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

@router.post("/{instance_id}/test", response_model=OracleConnectionTestResult)
async def test_instance(instance_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")

    pwd = decrypt_password(row.read_password)
    if not pwd:
        return OracleConnectionTestResult(
            ok=False, message="Stored password could not be decrypted."
        )

    mgr = OracleConnectionManager.get()
    try:
        conn = mgr.test_connection(
            host=row.host,
            port=row.port,
            service_name=row.service_name,
            user=row.read_user,
            password=pwd,
        )
    except OraclePermissionError as e:
        row.status = "forbidden"
        row.last_checked = datetime.utcnow()
        await db.commit()
        granted, missing = mgr.check_read_permissions(
            mgr.test_connection(  # try once more for role probe, ignore failure
                host=row.host, port=row.port, service_name=row.service_name,
                user=row.read_user, password=pwd,
            )
        )
        return OracleConnectionTestResult(
            ok=False, message=str(e), permissions=granted, missing_permissions=missing,
        )
    except OracleConnectionError as e:
        row.status = "down"
        row.last_checked = datetime.utcnow()
        await db.commit()
        return OracleConnectionTestResult(ok=False, message=str(e))

    # Connected — probe permissions.
    granted, missing = mgr.check_read_permissions(conn)
    ver = conn.version

    # Persist version + status.
    row.oracle_version = f"{ver.major}.{ver.minor}" if ver.major else None
    row.status = "up"
    row.last_checked = datetime.utcnow()
    await db.commit()

    # Close the test connection (it's not pooled).
    conn.close()

    return OracleConnectionTestResult(
        ok=len(missing) == 0,
        oracle_version=row.oracle_version,
        message=(
            f"Connected. Oracle {row.oracle_version or 'unknown'}. "
            f"{len(missing)} missing privilege(s)."
            if missing else
            f"Connected. Oracle {row.oracle_version or 'unknown'}. All required privileges present."
        ),
        permissions=granted,
        missing_permissions=missing,
    )


# ---------------------------------------------------------------------------
# Pool warming / lazy setup
# ---------------------------------------------------------------------------

@router.post("/{instance_id}/warm", response_model=OracleInstanceOut)
async def warm_instance(instance_id: int, db: AsyncSession = Depends(get_db)):
    """Pre-create the connection pool and probe Oracle version."""
    row = await db.get(OracleInstance, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    pwd = decrypt_password(row.read_password)
    if not pwd:
        raise HTTPException(status_code=400, detail="Decrypt of stored password failed")

    mgr = OracleConnectionManager.get()
    mgr.ensure_pool(
        instance_id,
        host=row.host,
        port=row.port,
        service_name=row.service_name,
        user=row.read_user,
        password=pwd,
    )
    ver = mgr.get_version(instance_id)
    if ver and ver.major:
        row.oracle_version = f"{ver.major}.{ver.minor}"
    row.status = "up"
    row.last_checked = datetime.utcnow()
    await db.commit()
    return row
