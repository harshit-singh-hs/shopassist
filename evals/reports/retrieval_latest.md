Retrieval eval: 70 questions, 307 chunks from 43 real pages, k=4

| Retriever | Hit@1 | Hit@4 | MRR | p50 latency |
|---|---|---|---|---|
| keyword | 57% | 84% | 0.677 | 0 ms |
| dense | 74% | 94% | 0.824 | 8 ms |
| hybrid | 70% | 94% | 0.801 | 7 ms |
| hybrid_rerank | 73% | 97% | 0.832 | 555 ms |

Misses for dense:
- delivery guy never came to collect my return  (top result: customer-service-frequently-asked-questions)
- how much do you charge for delivery  (top result: about-pay-on-delivery)
- someone called saying they are from amazon and asked for my OTP  (top result: secure-delivery-with-a-one-time-password)
- can a technician come home to check my TV  (top result: about-exchanges)
