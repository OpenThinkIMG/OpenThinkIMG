
import torch
from torch.utils.data import DataLoader,Dataset
from accelerate import Accelerator
import requests
import re
import copy

from ..models.abstract_model import tp_model
from .dynamic_batch_manager import DynamicBatchManager
from ..utils.utils import *
from ..utils.log_utils import get_logger
from ...tool_workers.tool_manager.base_manager import ToolManager
import torch.distributed as dist

logger = get_logger(__name__)
class BaseToolInferencer(object):
    def __init__(
        self,
        tp_model: tp_model = None,
        # dataset: Dataset = None,
        batch_size: int = 1,
        model_mode: str = "llava_plus",
        # controller_addr: str = None,
        max_rounds: int = 3,
        stop_token: str = "<stop>",
    ):
        self.accelerator = Accelerator()
        self.tp_model = tp_model
        self.model_mode = model_mode 
        self.generate_conversation_fn = self.tp_model.generate_conversation_fn
        self.append_conversation_fn = self.tp_model.append_conversation_fn
        
        if dist.is_initialized() and self.accelerator.device.type == "cuda" and not is_vllm_environment():
            self.tp_model = self.tp_model.to(self.accelerator.device)
            self.tp_model = self.tp_model.to(torch.bfloat16)

        self.batch_size = batch_size
        self.max_rounds = max_rounds
        self.stop_token = stop_token
        self.manager = DynamicBatchManager(
            batch_size=self.batch_size, 
            max_rounds=self.max_rounds, 
            stop_token=self.stop_token,
            generate_conversation_fn = self.tp_model.generate_conversation_fn,
        )
        self.tool_manager = ToolManager()
        self.available_models = self.tool_manager.available_tools
        # self.headers = {"User-Agent": "LLaVA-Plus Client"}
        
        # self.controller_addr = controller_addr
        # self.init_workers()
        
    ## Init Tool Workers
    # def init_workers(self):
    #     self.init_model_list()
    #     self.init_model_addr_dict()
    
    # def init_model_list(self):
    #     ret = requests.post(self.controller_addr + "/refresh_all_workers")
    #     assert ret.status_code == 200
    #     ret = requests.post(self.controller_addr + "/list_models")
    #     models = ret.json()["models"]
    #     logger.info(f"Models: {models}")
    #     self.available_models = models
        
    # def init_model_addr_dict(self):
    #     self.model_addr_dict = {}
    #     for model_name in self.available_models:
    #         ret = requests.post(self.controller_addr + "/get_worker_address",
    #                             json={"model": model_name})
    #         worker_addr = ret.json()["address"]
    #         if worker_addr == "":
    #             logger.error(f"worker_addr for {model_name} is empty")
    #             continue
    #         self.model_addr_dict[model_name] = worker_addr

    
    # def get_worker_addr(self, model_name):
    #     return self.model_addr_dict[model_name]
        
    

    ## Tool Response
    def batch_tool_response_to_next_round_input(self):
        current_batch = self.manager.get_current_batch()
        
        next_round_prompt = []
        for idx,item in enumerate(current_batch):
            if item.model_response is None or item.status != "processing":
                continue
            # breakpoint() 
            tool_cfg = item.tool_cfg[item.current_round-1]
            tool_response = item.tool_response[item.current_round-1]
            assert len(item.tool_cfg) == item.current_round 
            assert len(item.tool_response) == item.current_round 
            original_prompt = item.meta_data.get("text", "")
            
            if tool_response is not None:
                try:
                    if "edited_image" in tool_response:
                        edited_image = tool_response.pop("edited_image")

                        if self.model_mode == "llava_plus": 
                            edited_image = base64_to_pil(edited_image)
                        if self.model_mode == "general": 
                            edited_image = edited_image
                    else:
                        edited_image = None
                    
                    if "text" in tool_response:
                        tool_response_text = tool_response["text"]
                    else:
                        tool_response_text = None
                    # breakpoint() 
                    api_name = tool_cfg[0].get("API_name", tool_cfg[0].get("api_name", ""))

                    if self.model_mode == "llava_plus": 
                        new_response = f"{api_name} model outputs: {tool_response_text}\n\n"
                        new_round_prompt = f"{new_response} Please summarize the model outputs and answer my first question: {original_prompt}"
                    
                    if self.model_mode == "general":
                        new_response = f"OBSERVATION:\n{api_name} model outputs: {tool_response_text}\n"
                        new_round_prompt = f"{new_response}Please summarize the model outputs and answer my first question: {original_prompt}"                        
                except:
                    edited_image = None
                    new_round_prompt = original_prompt
            else:
                edited_image = None
                new_round_prompt = original_prompt
            new_round_input = dict(text=new_round_prompt,image=edited_image)
            item.new_round_input.append(new_round_input)
            item.conversation = self.append_conversation_fn(
                conversation=item.conversation, text=new_round_prompt, image=edited_image, role="user"
            )
            # breakpoint()

    
    def batch_get_tool_response(self):
        current_batch = self.manager.get_current_batch()
        for item in current_batch:
            if item.model_response is None or item.status != "processing":
                continue
            
            tool_cfg = item.tool_cfg[item.current_round-1]
            assert len(item.tool_cfg) == item.current_round

            image = item.meta_data.get("image", None)
            
            if tool_cfg is not None and len(tool_cfg) > 0:
                assert item.status == "processing"
                try:
                    assert len(tool_cfg) == 1, "Only one tool is supported for now, but got: {}".format(tool_cfg)

                    api_name = tool_cfg[0].get("API_name", tool_cfg[0].get("api_name", ""))
                    if api_name not in self.available_models:
                        if api_name == "Terminate":
                            logger.info(f"API_name is {api_name}. Finish!")
                            continue
                        else:
                            logger.error(f"API_name {api_name} not in available models, {self.available_models}")
                            item.tool_response.append(dict(text=f"There is no tool names {api_name}.",error_code=1))
                            continue

                    # if input contains image
                    if api_name in ['crop', 'drawline', "grounding", "DrawHorizontalLineByY", "Point", "DrawVerticalLineByX", "SegmentRegionAroundPoint", "ZoomInSubfigure", "OCR"]:
                        assert image is not None, "Image is required for tool: {}".format(api_name)
                        image = load_image(image)
                        image = pil_to_base64(image)
                        
                    else:
                        image = None

                    tool_cfg[0].get("api_params", tool_cfg[0].get("API_params",{})).pop('image', None)
                    api_params = tool_cfg[0].get("api_params",tool_cfg[0].get("API_params",{}))
                    
                    api_paras = {
                        "box_threshold": 0.3,
                        "text_threshold": 0.25,
                        **api_params,
                    }
                    
                    if image:
                        api_paras['image'] = image

                    tool_response = self.tool_manager.call_tool(api_name, api_paras)
                    tool_response_clone = copy.deepcopy(tool_response)


                    if "edited_image" in tool_response:
                        tool_response.pop("edited_image", None)
                    logger.info(f"tool_response: {tool_response}")
                    item.tool_response.append(tool_response_clone)
                    # breakpoint() 
                    continue
                    # return tool_response_clone
                except:
                    logger.info(f"Tool {api_name} failed to answer the question, tool_cfg is {tool_cfg}")
                    item.tool_response.append(dict(text=f"Tool {api_name} failed to answer the question.",error_code=1))
                    continue
                    # return dict(text=f"Tool {api_name} failed to answer the question.")
            else:
                item.tool_response.append(None)
                
                continue
            
    
    def batch_parse_tool_config(self):
        current_batch = self.manager.get_current_batch()
        for item in current_batch:
            model_response = item.model_response[item.current_round-1]
            assert len(item.model_response) == item.current_round
            
            if model_response is None or item.status != "processing":
                continue
            try:

                if self.model_mode == "general":
                    pattern = r'\{(?:[^{}]|\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})*\}'
                    match = re.search(pattern, model_response)
                    if match:
                        # Remove the outermost brackets and return the content inside
                        content = match.group(0)
                    else:
                        content = model_response
                    content = json.loads(content)

                    if len(content['actions']) >= 1:
                        action = content['actions'][0]
                        assert 'name' in action and 'arguments' in action, "missing 'name' or 'arguments' in the parsed action."
                        if action['name'] == "OCR":
                            tool_cfg = [{'API_name': action['name'],
                                        'API_params': {'param': action['arguments']['image']}}]                            
                        elif action['name'] == "Terminate":
                            tool_cfg = [{'API_name': action['name'],
                                        'API_params': {'param': action['arguments']['ans']}}]   
                        else:                                
                            tool_cfg = [{'API_name': action['name'],
                                    'API_params': {'param': action['arguments']['param']}}]
                    else:
                        tool_cfg = None
                    
                if self.model_mode == "llava_plus":
                    pattern = r'"thoughts🤔"(.*)"actions🚀"(.*)"value👉"(.*)'
                    matches = re.findall(pattern, model_response, re.DOTALL)
                    if len(matches) > 0:
                        try:
                            # [{'API_name': 'grounding', 'API_params': {'param': 'all lines'}}]
                            tool_cfg = json.loads(matches[0][1].strip())
                        except Exception as e:
                            tool_cfg = json.loads(
                                matches[0][1].strip().replace("\'", "\""))
                        logger.info(f"tool_cfg: {tool_cfg}")
                    else:
                        tool_cfg = None
            except Exception as e:
                logger.info(f"Failed to parse tool config: {e}. action list is {action}")
                tool_cfg = None
            item.tool_cfg.append(tool_cfg)

    
    
    ## Batch Inference
    def batch_inference(self,dataset):
        
        self.dataset = dataset
        self.dataloader = DataLoader(
            dataset, 
            batch_size=1, 
            num_workers=2, 
            collate_fn=lambda x: x[0]
        )
        if dist.is_initialized() and not is_vllm_environment():
            self.dataloader = self.accelerator.prepare(self.dataloader)
        self.dataloader_iter = iter(self.dataloader)
        self.tp_model.eval()

        progress_bar = tqdm_rank0(len(self.dataloader), desc="Model Responding")
        if len(self.dataloader) == 0:
            self.accelerator.wait_for_everyone()
            return
        
        # Generate the first batch
        # import debugpy
        # debugpy.listen(address = ('0.0.0.0', 7119))
        # debugpy.wait_for_client() 
        # breakpoint() #在下一句代码处暂停
        self.manager.append_item_to_full(self.dataloader_iter, progress_bar=progress_bar)
        current_batch = self.manager.get_current_batch()
        self.tp_model.generate(current_batch)
        # pending; processing and finishing
        self.manager.update_item_status()
        # import debugpy
        # debugpy.listen(address = ('0.0.0.0', 7119))
        # debugpy.wait_for_client() 
        # breakpoint() # 在下一句代码处暂停
        # dist.barrier()
        while len(current_batch) > 0:
            try:
                # Inspect and yield output
                results = self.manager.pop_qualified_items()
                for res in results:
                    idx = res["meta_data"]["idx"]
                    self.dataset.store_results(dict(idx=idx,results=res))
                
                # Parse tool config and generate tool response
                self.batch_parse_tool_config()
                
                self.batch_get_tool_response()
                self.batch_tool_response_to_next_round_input()
                
                # Refill the current batch
                self.manager.append_item_to_full(self.dataloader_iter,progress_bar=progress_bar)
                
                current_batch = self.manager.get_current_batch()
                self.tp_model.generate(current_batch)
                self.manager.update_item_status()
            except StopIteration:
                break
        assert len(self.manager.get_current_batch()) == 0
        self.accelerator.wait_for_everyone()
    