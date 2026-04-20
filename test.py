from openai import OpenAI
# vLLM 서버 설정
client = OpenAI(
    base_url="http://192.168.100.13:8000/v1",
    api_key="EMPTY"
) 
# 질문 요청 (stream=True 추가)
response = client.chat.completions.create(
    model="gemma-4-31b-it",
    messages=[
        {"role": "user", "content": "오늘의 환율이 얼마야?"}
    ],
    stream=True  # 스트리밍 활성화
)

print("답변: ", end="", flush=True)

# 스트리밍 데이터 출력 루프
for chunk in response:
    # 각 청크에서 텍스트 내용 추출
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)

print() # 마지막 줄바꿈