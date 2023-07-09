'''

A locally running LLM.

'''

from lsprotocol.types import (
    CodeAction,
    CodeActionKind,
    CodeActionParams,
    Command,
    Range,
    TextDocumentIdentifier,
)
import requests
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from thespian.actors import Actor
import logging
import argparse

from llmpal.edit import init_block, cleanup_block, BlockJob
from llmpal.common import extract_range, find_block, mk_logger
from llmpal.server import Server


START_TAG = ':START_LOCAL:'
END_TAG = ':END_LOCAL:'
NAME = 'local_llm'
log = mk_logger(NAME, logging.WARN)


class LocalLLMActor(Actor):
    def __init__(self):
        log.debug('ACTOR INIT')
        self.is_running = False
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.current_future = None
        self.should_stop = Event()
        self.tags = [START_TAG, END_TAG]

    def receiveMessage(self, msg, sender):
        command = msg.get('command')
        doc = msg.get('doc')

        edits = msg.get('edits')
        log.debug(f'''
%%%%%%%%%%
ACTOR RECV: {msg["command"]}
ACTOR STATE:
is_running: {self.is_running}
should stop: {self.should_stop.is_set()}
current_future: {self.current_future}

EDITS STATE:
job_thread alive: {edits.job_thread.is_alive() if edits and edits.job_thread else "NOT STARTED"}
%%%%%%%%%%''')

        ##########
        # Start
        if command == 'start':
            uri = msg.get('uri')
            range = msg.get('range')
            prompt = msg.get('prompt')
            edits = msg.get('edits')

            # check if block already exists
            start_ixs, end_ixs = find_block(START_TAG,
                                            END_TAG,
                                            doc)

            if not (start_ixs and end_ixs):
                init_block(NAME, self.tags, uri, range, edits)

            self.start(uri, range, prompt, edits)

        ##########
        # Stop
        elif command == 'stop':
            self.stop()

        ##########
        # Config
        elif command == 'set_config':
            self.model_path = msg.get('model_path')
            self.model_commit = msg.get('model_commit')
            self.local_max_length = msg.get('local_max_length')
            self.top_k = msg.get('top_k')
            self.llm_port = msg.get('llm_port')
            self.llm_uri = msg.get('llm_uri')

    def start(self, uri, range, prompt, edits):
        if self.is_running:
            log.info('WARN: ON_START_BUT_RUNNING')
            return
        log.debug('ACTOR START')

        self.is_running = True
        self.should_stop.clear()

        self.current_future = self.executor.submit(
            self.local_llm_stream_fn, uri, prompt, self.should_stop, edits
        )
        log.debug('START CAN RETURN')

    def stop(self):
        log.debug('ACTOR STOP')
        if not self.is_running:
            log.info('WARN: ON_STOP_BUT_STOPPED')

        self.should_stop.set()

        if self.current_future:
            self.current_future.result()  # block, wait to finish
            self.current_future = None
        log.debug('FINALLY STOPPED')

    def local_llm_stream_fn(self, uri, prompt, stop_event, edits):
        log.debug('START: LOCAL_LLM_STREAM_FN')
        try:
            request_data = {
                "text": prompt,
                "max_length": self.local_max_length,
                "do_sample": True,
                "top_k": self.top_k,
                "num_return_sequences": 1
            }
            # Stream response from LLM Server
            response = requests.post(f"{self.llm_uri}/local_llm_stream",
                                     json=request_data,
                                     stream=True)
            if response.status_code != 200:
                raise Exception(
                    f"POST request to {self.llm_uri} failed with status code "
                    f"{response.status_code}"
                )

            # Stream the results to LSP Client
            running_text = ''
            for line in response.iter_lines():
                # For breaking out early
                if stop_event.is_set():
                    log.debug('STREAM_FN received STOP EVENT')
                    break
                response_data = json.loads(line)
                new_text = response_data["generated_text"]
                log.debug(f'NEW: {new_text}')
                # ignore empty strings
                if len(new_text) == 0:
                    continue

                running_text += new_text
                job = BlockJob(
                    uri=uri,
                    start_tag=START_TAG,
                    end_tag=END_TAG,
                    text=f'\n{running_text}\n',
                    strict=False,
                )
                edits.add_job(NAME, job)

            # Streaming is done, and those added jobs were all
            # non-strict. Let's make sure to have one final strict
            # job. Streaming jobs are ok to be dropped, but we need to make
            # sure it does finalize, eg before a strict delete-tags job is
            # added.
            job = BlockJob(
                uri=uri,
                start_tag=START_TAG,
                end_tag=END_TAG,
                text=f'\n{running_text}\n',
                strict=True,
            )
            edits.add_job(NAME, job)

        except Exception as e:
            log.error(f'Error: Local LLM, {e}')

        # Cleanup
        log.debug('CLEANING UP')
        cleanup_block(NAME, self.tags, uri, edits)
        self.is_running = False
        self.current_future = None
        self.should_stop.clear()


def code_action_local_llm(params: CodeActionParams):
    text_document = params.text_document
    range = params.range
    return CodeAction(
        title='Local LLM',
        kind=CodeActionKind.Refactor,
        command=Command(
            title='Local LLM',
            command='command.localLlmStream',
            # Note: these arguments get jsonified, not passed directly
            arguments=[text_document, range]
        )
    )


##################################################
# External API

def configure(config_yaml):
    parser = argparse.ArgumentParser()

    parser.add_argument('--model_path', default=config_yaml.get('model_path', None))
    parser.add_argument('--model_commit', default=config_yaml.get('model_commit', None))
    parser.add_argument('--local_max_length', default=config_yaml.get('local_max_length', None))
    parser.add_argument('--top_k', default=config_yaml.get('top_k', None))
    parser.add_argument('--llm_port', default=config_yaml.get('llm_port', None))
    parser.add_argument('--llm_uri', default=config_yaml.get('llm_uri', None))

    # bc this is only concerned with local llm params, do not error if extra
    # params are sent via cli.
    args, _ = parser.parse_known_args()
    return args



def initialize(config, server):
    # Actor
    server.add_actor(NAME, LocalLLMActor)
    server.tell_actor(NAME, {
        'command': 'set_config',
        **vars(config)  # argparse.Namespace -> dict
    })

    # CodeActions
    server.add_code_action(code_action_local_llm)

    @server.thread()
    @server.command('command.localLlmStream')
    def local_llm_stream(ls: Server, args):
        text_document = ls.converter.structure(args[0], TextDocumentIdentifier)
        range = ls.converter.structure(args[1], Range)
        uri = text_document.uri
        doc = ls.workspace.get_document(uri)
        doc_source = doc.source

        # Extract the highlighted region
        prompt = extract_range(doc_source, range)

        # Send a message to start the stream
        actor_args = {
            'command': 'start',
            'uri': uri,
            'range': range,
            'prompt': prompt,
            'edits': ls.edits,
            'doc': doc_source,
        }
        ls.tell_actor(NAME, actor_args)
