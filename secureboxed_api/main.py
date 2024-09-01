import os
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import Response
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from shadow_drive import ShadowDriveClient
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from motor.motor_asyncio import AsyncIOMotorClient
import jwt
from datetime import datetime, timedelta

load_dotenv()

app = FastAPI()

# Initialize encryption key (you might want to use a more secure key management system)
KEY = AESGCM.generate_key(bit_length=128)

# Initialize Solana client
solana_client = AsyncClient(os.getenv("SOLANA_RPC_URL"))

# Initialize MongoDB client
mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = mongo_client.shadow_drive_db
users_collection = db.users

# JWT settings
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_MINUTES = 30

# OAuth2 scheme for token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Initialize ShadowDriveClient with the key from .env
shadow_drive_keypair = Keypair.from_base58_string(os.getenv("SHADOW_DRIVE_PRIVATE_KEY"))
shadow_drive_client = ShadowDriveClient(shadow_drive_keypair)

class UserSession:
    def __init__(self, public_key: Pubkey):
        self.public_key = public_key

async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        public_key: str = payload.get("sub")
        if public_key is None:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    
    user = await users_collection.find_one({"public_key": public_key})
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    
    return UserSession(Pubkey.from_string(public_key))

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRATION_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt

@app.post("/login")
async def login(public_key: str):
    # Check if user exists in the database
    user = await users_collection.find_one({"public_key": public_key})
    
    if user is None:
        # Save the new user to the database
        await users_collection.insert_one({"public_key": public_key})
    
    # Create access token
    access_token = create_access_token({"sub": public_key})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), user: UserSession = Depends(get_current_user)):
    contents = await file.read()
    aesgcm = AESGCM(KEY)
    nonce = os.urandom(12)
    encrypted_data = aesgcm.encrypt(nonce, contents, None)
    
    encrypted_filename = f"encrypted_{user.public_key}_{file.filename}"
    with open(encrypted_filename, "wb") as f:
        f.write(nonce + encrypted_data)
    
    urls = shadow_drive_client.upload_files([encrypted_filename])
    
    os.remove(encrypted_filename)
    
    return {"filename": file.filename, "url": urls[0]}

@app.get("/download/{filename}")
async def download_file(filename: str, user: UserSession = Depends(get_current_user)):
    current_files = shadow_drive_client.list_files()
    
    file_url = next((f for f in current_files if f.endswith(f"{user.public_key}_{filename}")), None)
    if not file_url:
        raise HTTPException(status_code=404, detail="File not found")
    
    encrypted_data = shadow_drive_client.get_file(file_url)
    
    nonce = encrypted_data[:12]
    encrypted_content = encrypted_data[12:]
    aesgcm = AESGCM(KEY)
    decrypted_data = aesgcm.decrypt(nonce, encrypted_content, None)
    
    return Response(content=decrypted_data, media_type="application/octet-stream")

@app.delete("/delete/{filename}")
async def delete_file(filename: str, user: UserSession = Depends(get_current_user)):
    current_files = shadow_drive_client.list_files()
    
    file_url = next((f for f in current_files if f.endswith(f"{user.public_key}_{filename}")), None)
    if not file_url:
        raise HTTPException(status_code=404, detail="File not found")
    
    shadow_drive_client.delete_files([file_url])
    
    return {"message": f"File {filename} deleted successfully"}

@app.get("/list_files")
async def list_files(user: UserSession = Depends(get_current_user)):
    all_files = shadow_drive_client.list_files()
    user_files = [f for f in all_files if f"{user.public_key}_" in f]
    return {"files": user_files}
