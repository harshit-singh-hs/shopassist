Model comparison: 70 questions, 307 chunks, k=4

| Embedder | Mode | Reranker | Hit@1 | Hit@4 | MRR | p50 latency |
|---|---|---|---|---|---|---|
| ibm-granite/granite-embedding-small-english-r2 | hybrid_rerank | Xenova/ms-marco-MiniLM-L-6-v2 | 73% | 97% | 0.832 | 566 ms |
| ibm-granite/granite-embedding-small-english-r2 | hybrid_rerank | Xenova/ms-marco-MiniLM-L-12-v2 | 71% | 96% | 0.829 | 882 ms |
| ibm-granite/granite-embedding-small-english-r2 | dense | - | 74% | 94% | 0.824 | 7 ms |
| ibm-granite/granite-embedding-small-english-r2 | hybrid_rerank | jinaai/jina-reranker-v1-turbo-en | 71% | 94% | 0.814 | 705 ms |
| ibm-granite/granite-embedding-small-english-r2 | hybrid | - | 70% | 94% | 0.801 | 8 ms |
| BAAI/bge-small-en-v1.5 | dense | - | 66% | 93% | 0.780 | 6 ms |
| BAAI/bge-small-en-v1.5 | hybrid | - | 69% | 90% | 0.773 | 6 ms |
| snowflake/snowflake-arctic-embed-s | hybrid | - | 54% | 86% | 0.675 | 6 ms |
| snowflake/snowflake-arctic-embed-s | dense | - | 39% | 64% | 0.487 | 6 ms |
