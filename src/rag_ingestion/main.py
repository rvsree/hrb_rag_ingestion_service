"""FastAPI entrypoint for rag_ingestion_service."""

from fastapi import FastAPI

from rag_ingestion.documents.routes import router

app = FastAPI(title="rag_ingestion_service")
app.include_router(router)


@app.get("/ping")
async def ping():
    return {"status": "ok"}
