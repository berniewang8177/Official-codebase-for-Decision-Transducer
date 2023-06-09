import numpy as np
import torch
import torch.nn as nn

from decision_transducer.models.model import TrajectoryModel
from decision_transducer.models.encoders import Encoder, get_lookahead_mask
from decision_transducer.models.join_net import JoinNet
from decision_transducer.models.biasing_combine import BiasCombineNet

class DecisionTransducer(TrajectoryModel):

    """
    This model uses causasl transformer encoder to model (Return-to-go_1, state_1, action_1, ...) separately
    """

    def __init__(
            self,
            state_dim,
            act_dim,
            hidden_size,
            n_layer = 3,
            max_length=None,
            max_ep_len=4096,
            action_tanh=True,
            pdrop = 0.1,
            bias_mode = 'b1',
            norm_mode = 'n1',
            c_mode = 'c22',
            modality_emb = 0,
            norm_joint = False,
            join_all = False,
            **kwargs
    ):
        super().__init__(state_dim, act_dim, max_length=max_length)

        self.hidden_size = hidden_size
        self.pdrop = pdrop
        self.pre_norm = True
        if norm_mode == 'n0':
            # no layer norm will be applied after state/action/Rtg embedding 
            pass
        else:
            self.embed_ln1 = nn.LayerNorm(hidden_size)
            if norm_mode != 'n1':
                self.embed_ln2 = nn.LayerNorm(hidden_size)
                self.embed_ln3 = nn.LayerNorm(hidden_size)

        # encoders for 3 modalities
        self.state_encoder = Encoder(hidden_size, n_layers = n_layer, pdrop = self.pdrop, pre_norm = self.pre_norm)
        self.action_encoder = Encoder(hidden_size, n_layers = n_layer, pdrop = self.pdrop, pre_norm = self.pre_norm)

        self.bias_mode = bias_mode
        self.norm_mode = norm_mode
        self.c_mode = c_mode

        if self.bias_mode != "b0":
            self.rtg_encoder = Encoder(hidden_size, n_layers = n_layer, pdrop = self.pdrop, pre_norm = self.pre_norm)

        # join network with postnorm 
        self.join = JoinNet(hidden_size, pdrop = self.pdrop, pre_norm = False,  norm_joint = norm_joint) #
        self.join_all = join_all

        # provide modality embedding before join net
        self.modality_emb = modality_emb
        if self.modality_emb > 0:
            self.mod_emb = nn.Embedding(modality_emb, self.hidden_size)

        # biasing amd combine
        self.bias1 = BiasCombineNet(hidden_size)
        self.bias2 = BiasCombineNet(hidden_size)

        self.embed_timestep = nn.Embedding(max_ep_len, hidden_size)
        self.embed_return = torch.nn.Linear(1, hidden_size)
        self.embed_state = torch.nn.Linear(self.state_dim, hidden_size)
        self.embed_action = torch.nn.Linear(self.act_dim, hidden_size)
        
        # note: we don't predict states or returns for the paper
        self.predict_action = nn.Sequential(
            *([nn.Linear(hidden_size, self.act_dim)] + ([nn.Tanh()] if action_tanh else []))
        )

        self._init_params()
    
    def _init_params(self):
        for p in self.parameters():
            if p.dim() > 1:
                torch.nn.init.xavier_normal_(p)

    def forward(self, states, actions, returns_to_go, timesteps, attention_mask=None, lens = None):

        batch_size, seq_length = states.shape[0], states.shape[1]
        # 1 means the position want to attend. Reverse it so that 1 means masking padding positions.
        attention_mask = ( 1.0 - attention_mask)
        attention_mask = attention_mask.bool()

        if attention_mask is None:
            # attention mask for GPT: 1 if can be attended to, 0 if not
            attention_mask = torch.zeros((batch_size, seq_length), dtype=torch.bool).to(states.device)

        # embed each modality with a different head

        state_embeddings = self.embed_state(states)
        # self.debug( state_embeddings, 'p1')
        action_embeddings = self.embed_action(actions)
        # self.debug( action_embeddings, 'p2')
        returns_embeddings = self.embed_return(returns_to_go)
        # self.debug( returns_embeddings, 'p3')
        time_embeddings = self.embed_timestep(timesteps)
        # self.debug( time_embeddings, 'p4')

        # time embeddings are treated similar to positional embeddings
        state_embeddings = state_embeddings + time_embeddings 
        action_embeddings = action_embeddings + time_embeddings 
        returns_embeddings =  returns_embeddings + time_embeddings
        
        # modality embedding
        if self.modality_emb > 0:
            returns_embeddings += self.mod_emb( torch.tensor(0).to(returns_embeddings.device) )
            state_embeddings += self.mod_emb( torch.tensor(1).to(state_embeddings.device) )
            action_embeddings += self.mod_emb(torch.tensor(2).to(action_embeddings.device) )

        # hard code modality sway for ablation
        # original s-r-a
        # now s-a-r, swap a-r
        # t = action_embeddings.clone()
        # action_embeddings = returns_embeddings.clone()
        # returns_embeddings = t

        # now a-s-r
        # _s = state_embeddings.clone()
        # _r = returns_embeddings.clone()
        # _a = action_embeddings.clone()

        # state_embeddings = _a
        # returns_embeddings = _s
        # action_embeddings = _r
        
        # layer norm before entering the model
        if self.norm_mode == 'n0':
            pass
        elif self.norm_mode == 'n1':
            returns_embeddings = self.embed_ln1( returns_embeddings )
            state_embeddings = self.embed_ln1( state_embeddings )
            action_embeddings = self.embed_ln1(  action_embeddings )
        elif self.norm_mode == 'n2':
            temp_stacked_inputs = torch.stack((returns_embeddings, state_embeddings, action_embeddings), dim = 1)
            # layer norm apply cross all modality
            temp_stacked_inputs = self.embed_ln1(temp_stacked_inputs)
            # self.debug( temp_stacked_inputs, 'p5')
            returns_embeddings = temp_stacked_inputs[:, 0]
            state_embeddings = temp_stacked_inputs[:, 1]
            action_embeddings = temp_stacked_inputs[:, 2]
        else:
            returns_embeddings = self.embed_ln1( returns_embeddings )
            state_embeddings = self.embed_ln2( state_embeddings )
            action_embeddings = self.embed_ln3(  action_embeddings )
        

        # encoding with causal mask
        causal_mask = get_lookahead_mask(state_embeddings)
        # self.debug( state_embeddings, 'before encoded state')
        encoded_state = self.state_encoder(state_embeddings, causal_mask, attention_mask)
        # self.debug( encoded_state, 'at encoded state', attention_mask)
        encoded_action = self.action_encoder(action_embeddings, causal_mask, attention_mask)
        # self.debug( encoded_action, 'at encoded_action')
        if self.bias_mode != "b0":
            encoded_rtg = self.rtg_encoder(returns_embeddings, causal_mask, attention_mask)

        # combiner & biasing
        if self.bias_mode == "b0":
            pass
        else:
            if self.bias_mode == "left" or self.bias_mode == "all":
            # bias state
                if self.c_mode == 'c22':
                    encoded_state = self.bias1.forward_22(encoded_state, encoded_rtg, causal_mask, attention_mask)
                elif self.c_mode == 'c21':
                    encoded_state = self.bias1.forward_21(encoded_state, encoded_rtg, causal_mask, attention_mask)
                else:
                    # c23: using Gate&add
                    encoded_state = self.bias1.forward_23(encoded_state, encoded_rtg, causal_mask, attention_mask)

            # bias state and also action
            if self.bias_mode == "right" or self.bias_mode == "all":
                if self.c_mode == 'c22':
                    encoded_action = self.bias2.forward_22(encoded_action, encoded_rtg, causal_mask, attention_mask)
                elif self.c_mode == 'c21':
                    encoded_action = self.bias2.forward_21(encoded_action, encoded_rtg, causal_mask, attention_mask)
                else:
                    # c23: using Gate&add
                    encoded_action = self.bias2.forward_23(encoded_action, encoded_rtg, causal_mask, attention_mask)
        
        # join network
        if self.join_all == False:
            join_encoded = self.join.forward(encoded_state, encoded_action, attention_mask)
        else:
            join_encoded = self.join.forward_all(encoded_rtg, encoded_state, encoded_action, attention_mask)

        # get predictions 
        action_preds = self.predict_action(join_encoded)  # predict next action given state
  
        return None, action_preds, None
    
    def debug(self, inp, str_, mask = None):
        with torch.no_grad():
            if True in torch.isnan( inp).detach().cpu().numpy():
                for idx, _ in enumerate(inp):
                    print(_)
                    if mask != None:
                        print(mask[idx])
                        print("--"*20)
                    print()
                assert False, f"At {str_}"

    def get_action(self, states, actions, rewards, returns_to_go, timesteps, **kwargs):
        
        # we don't care about the past rewards in this model

        states = states.reshape(1, -1, self.state_dim)
        actions = actions.reshape(1, -1, self.act_dim)
        returns_to_go = returns_to_go.reshape(1, -1, 1)
        timesteps = timesteps.reshape(1, -1)

        # true len exclude padding
        true_len = states.shape[1]
 
        if self.max_length is not None:
            states = states[:,-self.max_length:]
            actions = actions[:,-self.max_length:]
            returns_to_go = returns_to_go[:,-self.max_length:]
            timesteps = timesteps[:,-self.max_length:]

            # pad all tokens to sequence length
            # 1 means the position we want to attend. Reverse it for padding inside forward.
            attention_mask = torch.cat([ torch.ones(states.shape[1]), torch.zeros(self.max_length-states.shape[1]) ])
            # attention_mask = torch.cat([ torch.zeros(self.max_length-states.shape[1]), torch.ones(states.shape[1]),  ])
            attention_mask = attention_mask.to(dtype=torch.long, device=states.device).reshape(1, -1)
            states = torch.cat(
                [states, torch.zeros((states.shape[0], self.max_length-states.shape[1], self.state_dim), device=states.device)],
                dim=1).to(dtype=torch.float32)
            actions = torch.cat(
                [actions, torch.zeros((actions.shape[0], self.max_length - actions.shape[1], self.act_dim),
                            device=actions.device) ],
                dim=1).to(dtype=torch.float32)
            returns_to_go = torch.cat(
                [returns_to_go, torch.zeros((returns_to_go.shape[0], self.max_length-returns_to_go.shape[1], 1), device=returns_to_go.device), ],
                dim=1).to(dtype=torch.float32)
            timesteps = torch.cat(
                [timesteps, torch.zeros((timesteps.shape[0], self.max_length-timesteps.shape[1]), device=timesteps.device)],
                dim=1
            ).to(dtype=torch.long)

        else:
            attention_mask = None
        # assert False,f"{returns_to_go}\n{attention_mask}"
        _, action_preds, return_preds = self.forward(
            states, actions, returns_to_go, timesteps, attention_mask=attention_mask, **kwargs)
        # print( f"action_preds: {action_preds[0]}" )
        true_idx = min( self.max_length - 1, true_len - 1 )
        return action_preds[0, true_idx ]
