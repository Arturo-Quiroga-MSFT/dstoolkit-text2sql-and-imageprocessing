# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
from autogen_agentchat.conditions import (
    TextMentionTermination,
    MaxMessageTermination,
    SourceMatchTermination,
)
from autogen_agentchat.teams import SelectorGroupChat
from autogen_text_2_sql.creators.llm_model_creator import LLMModelCreator
from autogen_text_2_sql.creators.llm_agent_creator import LLMAgentCreator
import logging
from autogen_text_2_sql.custom_agents.parallel_query_solving_agent import (
    ParallelQuerySolvingAgent,
)
from autogen_agentchat.messages import TextMessage
import json
import os
from datetime import datetime
import re

from text_2_sql_core.payloads.interaction_payloads import (
    QuestionPayload,
    AnswerWithSourcesPayload,
    DismabiguationRequestPayload,
    ProcessingUpdatePayload,
    InteractionPayload,
    PayloadType,
)
from autogen_agentchat.base import TaskResult
from typing import AsyncGenerator


class AutoGenText2Sql:
    def __init__(self, **kwargs: dict):
        self.target_engine = os.environ["Text2Sql__DatabaseEngine"].upper()
        self.kwargs = kwargs

    def get_all_agents(self):
        """Get all agents for the complete flow."""
        # Get current datetime for the Query Rewrite Agent
        current_datetime = datetime.now()

        self.question_rewrite_agent = LLMAgentCreator.create(
            "question_rewrite_agent", current_datetime=current_datetime
        )

        self.parallel_query_solving_agent = ParallelQuerySolvingAgent(**self.kwargs)

        self.answer_agent = LLMAgentCreator.create("answer_agent")

        agents = [
            self.question_rewrite_agent,
            self.parallel_query_solving_agent,
            self.answer_agent,
        ]

        return agents

    @property
    def termination_condition(self):
        """Define the termination condition for the chat."""
        termination = (
            TextMentionTermination("TERMINATE")
            | SourceMatchTermination("answer_agent")
            | TextMentionTermination("requires_user_information_request")
            | MaxMessageTermination(5)
        )
        return termination

    def unified_selector(self, messages):
        """Unified selector for the complete flow."""
        logging.info("Messages: %s", messages)
        current_agent = messages[-1].source if messages else "user"
        decision = None

        # If this is the first message start with question_rewrite_agent
        if current_agent == "user":
            decision = "question_rewrite_agent"
        # Handle transition after query rewriting
        elif current_agent == "question_rewrite_agent":
            decision = "parallel_query_solving_agent"
        # Handle transition after parallel query solving
        elif current_agent == "parallel_query_solving_agent":
            decision = "answer_agent"

        if decision:
            logging.info(f"Agent transition: {current_agent} -> {decision}")
        else:
            logging.info(f"No agent transition defined from {current_agent}")

        return decision

    @property
    def agentic_flow(self):
        """Create the unified flow for the complete process."""
        flow = SelectorGroupChat(
            self.get_all_agents(),
            allow_repeated_speaker=False,
            model_client=LLMModelCreator.get_model("4o-mini"),
            termination_condition=self.termination_condition,
            selector_func=self.unified_selector,
        )
        return flow

    def extract_disambiguation_request(
        self, messages: list
    ) -> DismabiguationRequestPayload:
        """Extract the disambiguation request from the answer."""
        disambiguation_request = messages[-1].content
        return DismabiguationRequestPayload(
            disambiguation_request=disambiguation_request,
        )

    def parse_message_content(self, content):
        """Parse different message content formats into a dictionary."""
        if isinstance(content, (list, dict)):
            # If it's already a list or dict, convert to JSON string
            return json.dumps(content)

        # Try to extract JSON from markdown-style code blocks
        json_match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try parsing as regular JSON
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # If all parsing attempts fail, return the content as-is
        return content

    def extract_answer_payload(self, messages: list) -> AnswerWithSourcesPayload:
        """Extract the sources from the answer."""
        answer = messages[-1].content
        sql_query_results = self.parse_message_content(messages[-2].content)

        try:
            if isinstance(sql_query_results, str):
                sql_query_results = json.loads(sql_query_results)
        except json.JSONDecodeError:
            logging.warning("Unable to read SQL query results: %s", sql_query_results)
            sql_query_results = {}
            sub_question_results = {}
        else:
            # Only load sub-question results if we have a database result
            sub_question_results = self.parse_message_content(messages[1].content)
            logging.info("Sub-Question Results: %s", sub_question_results)

        try:
            sub_questions = [
                sub_question
                for sub_question_group in sub_question_results.get("sub_questions", [])
                for sub_question in sub_question_group
            ]

            logging.info("SQL Query Results: %s", sql_query_results)
            payload = AnswerWithSourcesPayload(
                answer=answer, sub_questions=sub_questions
            )

            if not isinstance(sql_query_results, dict):
                logging.error(f"Expected dict, got {type(sql_query_results)}")
                return payload

            if "results" not in sql_query_results:
                logging.error("No 'results' key in sql_query_results")
                return payload

            for question, sql_query_result_list in sql_query_results["results"].items():
                if not sql_query_result_list:  # Check if list is empty
                    logging.warning(f"No results for question: {question}")
                    continue

                for sql_query_result in sql_query_result_list:
                    if not isinstance(sql_query_result, dict):
                        logging.error(
                            f"Expected dict for sql_query_result, got {type(sql_query_result)}"
                        )
                        continue

                    if (
                        "sql_query" not in sql_query_result
                        or "sql_rows" not in sql_query_result
                    ):
                        logging.error("Missing required keys in sql_query_result")
                        continue

                    source = AnswerWithSourcesPayload.Body.Source(
                        sql_query=sql_query_result["sql_query"],
                        sql_rows=sql_query_result["sql_rows"],
                    )
                    payload.body.sources.append(source)

            if not payload.body.sources:
                logging.error("No valid sources extracted")

            return payload

        except Exception as e:
            logging.error("Error processing results: %s", str(e))
            # Return payload with error context instead of empty
            return AnswerWithSourcesPayload(
                answer=f"{answer}\nError processing results: {str(e)}"
            )

    async def process_question(
        self,
        question_payload: QuestionPayload,
        chat_history: list[InteractionPayload] = None,
    ) -> AsyncGenerator[InteractionPayload, None]:
        """Process the complete question through the unified system.

        Args:
        ----
            task (str): The user question to process.
            chat_history (list[str], optional): The chat history. Defaults to None.
            injected_parameters (dict, optional): Parameters to pass to agents. Defaults to None.

        Returns:
        -------
            dict: The response from the system.
        """
        logging.info("Processing question: %s", question_payload.body.question)
        logging.info("Chat history: %s", chat_history)

        agent_input = {
            "question": question_payload.body.question,
            "chat_history": {},
            "injected_parameters": question_payload.body.injected_parameters,
        }

        if chat_history is not None:
            # Update input
            for idx, chat in enumerate(chat_history):
                if chat.root.payload_type == PayloadType.QUESTION:
                    # For now only consider the user query
                    chat_history_key = f"chat_{idx}"
                    agent_input[chat_history_key] = chat.root.body.question

        async for message in self.agentic_flow.run_stream(task=json.dumps(agent_input)):
            logging.debug("Message: %s", message)

            payload = None

            if isinstance(message, TextMessage):
                if message.source == "question_rewrite_agent":
                    payload = ProcessingUpdatePayload(
                        message="Rewriting the query...",
                    )
                elif message.source == "parallel_query_solving_agent":
                    payload = ProcessingUpdatePayload(
                        message="Solving the query...",
                    )
                elif message.source == "answer_agent":
                    payload = ProcessingUpdatePayload(
                        message="Generating the answer...",
                    )

            elif isinstance(message, TaskResult):
                # Now we need to return the final answer or the disambiguation request
                logging.info("TaskResult: %s", message)

                if message.messages[-1].source == "answer_agent":
                    # If the message is from the answer_agent, we need to return the final answer
                    payload = self.extract_answer_payload(message.messages)
                elif message.messages[-1].source == "parallel_query_solving_agent":
                    # Load into disambiguation request
                    payload = self.extract_disambiguation_request(message.messages)
                elif message.messages[-1].source == "question_rewrite_agent":
                    # Load into empty response
                    payload = AnswerWithSourcesPayload(
                        answer="Apologies, I cannot answer that question as it is not relevant. Please try another question or rephrase your current question."
                    )
            else:
                logging.error("Unexpected TaskResult: %s", message)
                raise ValueError("Unexpected TaskResult")

            if payload is not None:
                logging.debug("Payload: %s", payload)
                yield payload
