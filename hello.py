"""Quick check that the G09 workspace key works."""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

load_dotenv()

# Workshop rule: use Haiku unless told to switch to claude-sonnet-5-5
llm = ChatAnthropic(model="claude-haiku-5-5", max_tokens=100)

print(llm.invoke("Say hello in one short sentence.").content)
