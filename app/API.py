from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
import uvicorn
import os
import docx
import PyPDF2
import tempfile
import google.generativeai as genai
import re
import time
from typing import List
from fastapi.middleware.cors import CORSMiddleware
from openpyxl import Workbook, load_workbook
from pydantic import BaseModel
from azure.storage.blob import BlobClient
import pandas as pd
from io import BytesIO

# Your SAS URL to the blob
sas_url = "https://gaigkyc.blob.core.windows.net/l2-technical/l2-technical_details.xlsx?sp=racw&st=2025-09-29T05:53:30Z&se=2026-12-03T14:08:30Z&sv=2024-11-04&sr=b&sig=6GnCcdMaPI4Tr805zc4xhTG5S%2B8gL9CcvxjlXkE15Yc%3D"

# Create BlobClient from SAS URL
blob_client = BlobClient.from_blob_url(sas_url)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # frontend URL
    allow_credentials=True,
    allow_methods=["*"],  # or restrict to ["POST"]
    allow_headers=["*"],)

# Configuration: Add your API keys here
API_KEYS = [
    "AIzaSyDR6Qt7525FZDUkVv7mevxqfLsM_22nN7M",
    "AIzaSyBcR6rMwP9v8e2cN56gdnkWMhJtOWyP_uU",
    "AIzaSyBH27G69SVWBCA4HwfhIJvkfvKz",
    "AIzaSyBseF57HxFiO_qOjCUqoqoXZRAY2Monmzw"
    # Add more API keys as needed
]

# Global variable to track current API key index
current_api_key_index = 0

def get_next_api_key():
    """Rotate to next API key and return it"""
    global current_api_key_index
    current_api_key_index = (current_api_key_index + 1) % len(API_KEYS)
    return API_KEYS[current_api_key_index]


EXCEL_FILE = "interview_data.xlsx"
if not os.path.exists(EXCEL_FILE):
    wb = Workbook()
    ws = wb.active
    ws.title = "Interviews"
    ws.append(["Interviewee Email", "Candidate Name", "Job Role", "iMocha Score"])
    wb.save(EXCEL_FILE)


class InterviewData(BaseModel):
    interviewee_email: str
    candidate_name: str
    job_role: str
    imocha_score: str



def call_gemini_with_retry(prompt: str, max_retries: int = 3) -> str:
    """Call Gemini API with automatic key rotation on rate limit errors"""
    global current_api_key_index
    
    for attempt in range(max_retries):
        try:
            # Configure with current API key
            genai.configure(api_key=API_KEYS[current_api_key_index])
            model = genai.GenerativeModel('gemini-2.5-flash')
            
            response = model.generate_content(prompt)
            return response.text
            
        except Exception as e:
            error_message = str(e).lower()
            
            # Check for rate limit or forbidden errors
            if "429" in error_message or "403" in error_message or "quota" in error_message or "rate limit" in error_message:
                print(f"Rate limit/Quota exceeded for API key {current_api_key_index + 1}. Rotating to next key...")
                
                # Rotate to next API key
                get_next_api_key()
                
                # Add a small delay before retry
                time.sleep(1)
                
                if attempt < max_retries - 1:
                    continue
                else:
                    return f"All API keys exhausted. Error: {str(e)}"
            else:
                # For other errors, don't retry
                return f"Error: {str(e)}"
    
    return "Maximum retries exceeded"


def extractimochascore(imocha_text: str) -> str:
    # Pattern to extract just the percentage
    patterns = [
        r"Proficient\s*\((\d+%)\)",  # Captures "83%" from "Proficient (83%)"
        r"scored\s+(\d+%)",  # Alternative: captures percentage after "scored"
        r"Score:\s*\d+\s*/\s*\d+.*?(\d+%)",  # Captures percentage after score format
        r"(\d{1,3}%)"  # Generic percentage pattern as fallback
    ]
    
    for pattern in patterns:
        match = re.search(pattern, imocha_text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)
    
    return "Score not found"


def extractcandidatename(resume_text: str) -> str:
    prompt = f"Extract the candidate name from the following resume text: {resume_text}"
    return call_gemini_with_retry(prompt)

def extractjobrole(jd_text: str) -> str:
    # Method 1: Simple regex to extract job title after "Job Description:" or similar patterns
    patterns = [
        r"Job Description:\s*([^\n]+)",
        r"Position:\s*([^\n]+)",
        r"Role:\s*([^\n]+)",
        r"Title:\s*([^\n]+)",
        r"^([^:\n]+?)(?:\s*\n|$)"  # Captures everything until newline, without removing after dash
    ]
    
    for pattern in patterns:
        match = re.search(pattern, jd_text.strip(), re.IGNORECASE | re.MULTILINE)
        if match:
            job_role = match.group(1).strip()
            # Don't remove everything after dash - keep the full role
            return job_role
    
    # If no pattern matches, try to get the first meaningful line
    lines = jd_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if line and len(line) > 3:  # Skip very short lines
            # Remove common prefixes but keep the full role
            line = re.sub(r'^(Job Description:|Position:|Role:|Title:)\s*', '', line, flags=re.IGNORECASE)
            return line.strip()
    
    return "Job role not found"

# --- Helper: Extract text from different file types ---
def extract_text_from_file(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".docx":
        doc = docx.Document(file_path)
        return "\n".join([para.text for para in doc.paragraphs])

    elif ext == ".pdf":
        text = ""
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() or ""
        return text

    else:
        return f"Unsupported file type: {ext}"

# --- API Endpoint ---
@app.post("/upload")
async def upload_files(
    resume: UploadFile = File(...),
    jd: UploadFile = File(...),
    imocha: UploadFile = File(...)
):
    results = {}
    response = {}

    for file_obj, name in [(resume, "resume"), (jd, "jd"), (imocha, "imocha")]:
        try:
            # Save file temporarily
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file_obj.filename)[1]) as tmp:
                tmp.write(await file_obj.read())
                tmp_path = tmp.name

            # Extract text
            results[name] = extract_text_from_file(tmp_path)

            # Clean up
            os.remove(tmp_path)

        except Exception as e:
            results[name] = f"Error processing {file_obj.filename}: {str(e)}"
    
    name = extractcandidatename(results['resume'])  # Fixed: uncommented this line
    role = extractjobrole(results['jd'])
    imocha_score = extractimochascore(results['imocha'])
    
    response['name'] = name
    response['role'] = role
    response['imocha_score'] = imocha_score
    return JSONResponse(response)


@app.post("/append/") # Load workbook
def add_data_to_excel(new_data: InterviewData):
    """
    Add new data to the Excel file in Azure Blob Storage
    new_data should be a Pydantic model with interview data
    """
    try:
        # Download current data
        blob_data = blob_client.download_blob().readall()
        df = pd.read_excel(BytesIO(blob_data))
        
        # Convert Pydantic model to dictionary
        data_dict = new_data.model_dump()
        
        # Create new row with proper column mapping
        new_row_data = {
            "Interviewee Email": data_dict["interviewee_email"],
            "Candidate Name": data_dict["candidate_name"], 
            "Job Role": data_dict["job_role"],
            "iMocha Score": data_dict["imocha_score"]
        }
        
        # Add new row
        new_row = pd.DataFrame([new_row_data])
        df = pd.concat([df, new_row], ignore_index=True)
        
        # Convert to Excel bytes
        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Interview_Data')
        
        excel_buffer.seek(0)
        
        # Upload updated file
        blob_client.upload_blob(excel_buffer.read(), overwrite=True)
        print("Data added successfully!")
        return {"status": "success", "message": "Data added to Excel successfully"}
        
    except Exception as e:
        print(f"Error adding data: {e}")
        return {"status": "error", "message": f"Error adding data: {str(e)}"}


if __name__ != "__main__":
    # This is for Vercel deployment
    handler = app


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)