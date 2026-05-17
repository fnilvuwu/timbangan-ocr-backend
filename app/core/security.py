from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def create_access_token(subject: str, role: str) -> str:
    expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {"sub": subject, "role": role, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc

def create_ocr_token(extracted_numbers: list[str]) -> str:
    expires_delta = timedelta(hours=1)
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {"extracted_numbers": extracted_numbers, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

def verify_ocr_token(token: str, weight: float) -> bool:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
        extracted = payload.get("extracted_numbers", [])
        weight_str = str(weight)
        # Check if the float representation matches any string in the extracted array
        # or if the string itself is in the array
        for num_str in extracted:
            try:
                if float(num_str) == weight:
                    return True
            except ValueError:
                pass
        return False
    except JWTError:
        return False
