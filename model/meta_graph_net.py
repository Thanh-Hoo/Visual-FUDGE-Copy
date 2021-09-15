import torch
from torch import nn
import torch.nn.functional as F
import math
import json
from .net_builder import make_layers, getGroupSize
from .attention import MultiHeadedAttention
from torch_geometric.nn import MetaLayer
from torch_scatter import scatter_mean
import numpy as np
import logging
import random

class MetaGraphFCEncoderLayer(nn.Module):
    def __init__(self, feat,edgeFeat,ch):
        super(MetaGraphFCEncoderLayer, self).__init__()

        self.node_layer = nn.Linear(feat,ch)
        if type(edgeFeat) is int and edgeFeat>0:
            self.edge_layer = nn.Linear(edgeFeat,ch)
            self.hasEdgeInfo=True
        else:
            self.edge_layer = nn.Linear(feat*2,ch)
            self.hasEdgeInfo=False

    def forward(self, input): 
        node_features, edge_indexes, edge_features, u_features = input
        node_featuresN = self.node_layer(node_features)
        if not self.hasEdgeInfo:
            edge_features = node_features[edge_indexes].permute(1,0,2).reshape(edge_indexes.size(1),-1)
        edge_features = self.edge_layer(edge_features)


        return node_featuresN, edge_indexes, edge_features, u_features

class EdgeFunc(nn.Module):
    def __init__(self,ch,dropout=0.1,norm='group',useRes=True,useGlobal=False,hidden_ch=None,soft_prune_edges=False,edge_decider=None,rcrhdn_size=0,avgEdges=False,sep_norm=False):
        super(EdgeFunc, self).__init__()
        self.soft_prune_edges=soft_prune_edges
        self.res=useRes
        self.avgEdges=avgEdges
        self.sep_norm = sep_norm

        if rcrhdn_size!=0:
            if rcrhdn_size<0:
                rcrhdn_size*=-1
                rcrhdn_size_out=0
                self.use_rcrhdn='gru'
            else:
                #use a special memory channel for recurrent applications of this layer
                self.use_rcrhdn = True
                rcrhdn_size_out=rcrhdn_size
            self.rcrhdn_size = rcrhdn_size
            self.rcrhdn_edges=None
        else:
            self.rcrhdn_size = 0
            rcrhdn_size_out = 0
            self.use_rcrhdn = False
        if hidden_ch is None:
            hidden_ch=ch+self.rcrhdn_size

        edge_in=3
        self.useGlobal=useGlobal
        if useGlobal:
            edge_in +=1

        actS=[]
        actM=[]
        actR=[]
        actP=[]
        acts = [actS,actM,actR,actP]

        if self.sep_norm:
            self.source_norm = nn.GroupNorm(getGroupSize(ch),ch)
            self.target_norm = nn.GroupNorm(getGroupSize(ch),ch)
            self.edge_norm = nn.GroupNorm(getGroupSize(ch),ch)
        
        if 'group' in norm:
            if not self.sep_norm:
                actS.append(nn.GroupNorm(getGroupSize(edge_in*ch+rcrhdn_size,edge_in*8),edge_in*ch+rcrhdn_size))
            actM.append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
            if self.use_rcrhdn:
                actR.append(nn.GroupNorm(getGroupSize(ch),ch))
            if self.soft_prune_edges and edge_decider is None:
                actP.append(nn.GroupNorm(getGroupSize(ch),ch))
        elif norm:
            raise NotImplemented('Norm: {}, not implmeneted for EdgeFunc'.format(norm))
        if dropout is not None:
            if type(dropout) is float or dropout==0:
                da=dropout
            else:
                da=0.1
            for act in acts:
                act.append(nn.Dropout(p=da,inplace=True))
        for act in acts:
            act.append(nn.ReLU(inplace=True))

        self.edge_mlp = nn.Sequential(*(actS),nn.Linear(ch*edge_in+rcrhdn_size, hidden_ch), *(actM), nn.Linear(hidden_ch, ch+rcrhdn_size_out))

        if self.soft_prune_edges:
            if edge_decider is None:
                self.edge_decider = nn.Sequential(*(actP),nn.Linear(ch, 1), nn.Sigmoid())
                # we shift the mean up to bias keeping edges (should help begining of training
                self.edge_decider[len(actP)].bias = nn.Parameter(self.edge_decider[len(actP)].bias.data + 2.0/self.edge_decider[len(actP)].bias.size(0))
            else:
                # we shouldn't need that bias here since it's already getting trained
                self.edge_decider = nn.Sequential(edge_decider, SharpSigmoid(-1))
        else:
            self.edge_decider = None
        if self.use_rcrhdn:
            #these layers are for providing initial values when cold-starting the recurrent net
            self.start_rcrhdn_edges = nn.Sequential(*actR,nn.Linear(ch,self.rcrhdn_size))
            if self.use_rcrhdn=='gru':
                self.edge_rcr = nn.GRU(input_size=ch*edge_in,hidden_size=rcrhdn_size,num_layers=1)

    def clear(self):
        self.rcrhdn_edges=None

    def forward(self, source, target, edge_attr, u, batch=None):
        if self.use_rcrhdn and self.rcrhdn_edges is None:
            self.rcrhdn_edges = self.start_rcrhdn_edges(edge_attr)
        # source, target: [E, F_x], where E is the number of edges.
        # edge_attr: [E, F_e]
        # u: [B, F_u], where B is the number of graphs.
        if u is not None:
            assert(u.size(0)==1)
            assert(not self.sep_norm)
            us = u.expand(source.size(0),u.size(1))
            out = torch.cat([source, target, edge_attr,us], dim=1)
        else:
            if self.sep_norm:
                source = self.source_norm(source)
                target = self.target_norm(target)
                edge_attr = self.edge_norm(edge_attr)
            out = torch.cat([source, target, edge_attr], dim=1)
        if self.use_rcrhdn=='gru':
            self.rcrhdn_edges = self.edge_rcr(out[None,...], self.rcrhdn_edges[None,...])[1][0]
        if self.use_rcrhdn:
            out = torch.cat([out,self.rcrhdn_edges],dim=1)
        out = self.edge_mlp(out)
        if self.use_rcrhdn and self.use_rcrhdn!='gru':
            self.rcrhdn_edges=out[:,-self.rcrhdn_size:]
            out=out[:,:-self.rcrhdn_size]
            

        if self.soft_prune_edges:
            pruneDecision = self.edge_decider(out)
            #print(pruneDecision)
            out *= self.soft_prune_edges
        if self.res:
            out+=edge_attr
        if self.avgEdges: #assumes bidirection edges repeated in order
            avg = (out[:out.size(0)//2] + out[out.size(0)//2:])/2
            out = avg.repeat(2,1)

        return out

class NodeTreeFunc(nn.Module):
    def __init__(self,ch,heads=4,dropout=0.1,norm='group',useRes=True,useGlobal=False,hidden_ch=None,rcrhdn_size=0):
        super(NodeTreeFunc, self).__init__()
        self.res=useRes

        if rcrhdn_size!=0:
            if rcrhdn_size<0:
                rcrhdn_size*=-1
                rcrhdn_size_out=0
                self.use_rcrhdn='gru'
            else:
                #use a special memory channel for recurrent applications of this layer
                self.use_rcrhdn = True
                rcrhdn_size_out=rcrhdn_size
            self.rcrhdn_size = rcrhdn_size
            self.rcrhdn_nodes=None
        else:
            self.rcrhdn_size = 0
            rcrhdn_size_out = 0
            self.use_rcrhdn = False
        if hidden_ch is None:
            hidden_ch=ch+self.rcrhdn_size

        node_in=2
        self.useGlobal=useGlobal
        if useGlobal:
            node_in+=1
        actS=[]
        actM=[]
        actR=[]
        actE=[]
        act1Step=[]
        act2Step=[]
        acts=[actS,actM,actR,actE,act1Step,act2Step]
        if 'group' in norm:
            actS.append(nn.GroupNorm(getGroupSize(node_in*ch+rcrhdn_size,node_in*8),node_in*ch+rcrhdn_size))
            actM.append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
            if self.use_rcrhdn:
                actR.append(nn.GroupNorm(getGroupSize(ch),ch))
            actE.append(nn.GroupNorm(getGroupSize(ch*2),ch*2))
            act1Step.append(nn.GroupNorm(getGroupSize(ch*3),ch*3))
            act2Step.append(nn.GroupNorm(getGroupSize(ch*2),ch*2))
        elif norm:
            raise NotImplemented('Norm: {}, not implmeneted for NodeTreeF'.format(norm))
        if dropout is not None:
            if type(dropout) is float or dropout==0:
                da=dropout
            else:
                da=0.1
            for act in acts:
                act.append(nn.Dropout(p=da,inplace=True))
        for act in acts:
            act.append(nn.ReLU(inplace=True))


        self.node_mlp = nn.Sequential(*actS,nn.Linear(ch*node_in+rcrhdn_size, hidden_ch), *(actM), nn.Linear(hidden_ch, ch+rcrhdn_size_out))
        if self.use_rcrhdn:
            #these layers are for providing initial values when cold-starting the recurrent net
            self.start_rcrhdn_nodes = nn.Sequential(*actR,nn.Linear(ch,self.rcrhdn_size))
            if self.use_rcrhdn=='gru':
                self.node_rcr = nn.GRU(input_size=ch*node_in,hidden_size=rcrhdn_size,num_layers=1)
        self.sum_encode = nn.Sequential(*actE,nn.Linear(2*ch,ch))
        self.sum_step = nn.Sequential(*act1Step,nn.Linear(3*ch,2*ch),*act2Step,nn.Linear(2*ch,ch))

    def clear(self):
        self.rcrhdn_nodes=None

    def summerize(self,data,x):
        if data.size(0)==0:
            return torch.zeros(1,x.size(0)).to(x.device)
        data = self.sum_encode(torch.cat((data,x.view(1,-1).expand(data.size(0),-1)),dim=1))
        oddout=None
        while data.size(0)>1 or oddout is not None:
            if oddout is not None:
                if (data.size(0)+oddout.size(0))%2==0:
                    data = torch.cat((data,oddout),dim=0)
                    oddout = None#torch.FloatTensor
                elif oddout.size(0)>1:
                    data = torch.cat((data,oddout[:-1]),dim=0)
                    oddout = oddout[-1:]
            else:
                if data.size(0)%2==1:
                    oddout = data[-1:]
                    data = data[:-1]
            paired = data.view(data.size(0)//2,data.size(1)*2)
            x_e = x.view(1,-1).expand(paired.size(0),-1)
            cated = torch.cat((paired,x_e),dim=1)
            #cated=cated.clone()
            #return cated[0:1,-x.size(0):]
            data = self.sum_step(cated)
            #data = cated[:,:x.size(0)]+cated[:,x.size(0):2*x.size(0)]+cated[:,-x.size(0):]
            #return data[0:1]
            
                #data = paired.view(data.size(0),data.size(1))
        return data

    def forward(self, x, edge_index, edge_attr, u, batch=None):
        if self.use_rcrhdn and self.rcrhdn_nodes is None:
            self.rcrhdn_nodes = self.start_rcrhdn_nodes(x)
        # x: [N, F_x], where N is the number of nodes.
        # edge_index: [2, E] with max entry N - 1.
        # edge_attr: [E, F_e]
        # u: [B, F_u]
        edgeLists=[[] for i in range(x.size(0))]
        row, col = edge_index
        for i in range(col.size(0)):
            edgeLists[col[i]].append(i)#edge_attr[:,
        if self.train:
            for node in range(x.size(0)):
                random.shuffle(edgeLists[node])
        #summary = torch.FloatTensor(x.size(0),edge_attr.size(1)).to(x.device)
        summary=[]
        for node in range(x.size(0)): #TODO this can be optimized by doing everything at once and doing book keeping
            summary.append( self.summerize(edge_attr[edgeLists[node]],x[node]) )
        summary=torch.cat(summary,dim=0)
        if u is not None:
            assert(u.size(0)==1)
            us = u.expand(source.size(0),u.size(1))
            out = torch.cat([x, summary,us], dim=1)
        else:
            out = torch.cat([x, summary], dim=1)
        if self.use_rcrhdn=='gru':
            self.rcrhdn_nodes = self.node_rcr(out[None,...], self.rcrhdn_nodes[None,...])[1][0]
        if self.use_rcrhdn:
            out = torch.cat([out,self.rcrhdn_nodes],dim=1)
        out = self.node_mlp(out)
        if self.use_rcrhdn and self.use_rcrhdn!='gru':
            self.rcrhdn_nodes=out[:,-self.rcrhdn_size:]
            out=out[:,:-self.rcrhdn_size]
            
        if self.res:
            out+=x
        return out

class NodeAttFunc(nn.Module):
    def __init__(self,ch,heads=4,dropout=0.1,norm='group',useRes=True,useGlobal=False,hidden_ch=None,agg_thinker='cat',rcrhdn_size=0,relu_node_act=False,att_mod=False,more_norm=False):
        super(NodeAttFunc, self).__init__()
        self.thinker=agg_thinker
        self.res=useRes
        self.norm_before_att=more_norm

        if rcrhdn_size!=0:
            if rcrhdn_size<0:
                rcrhdn_size*=-1
                rcrhdn_size_out=0
                self.use_rcrhdn='gru'
            else:
                #use a special memory channel for recurrent applications of this layer
                self.use_rcrhdn = True
                rcrhdn_size_out=rcrhdn_size
            self.rcrhdn_size = rcrhdn_size
            self.rcrhdn_nodes=None
        else:
            self.rcrhdn_size = 0
            rcrhdn_size_out = 0
            self.use_rcrhdn = False
        if hidden_ch is None:
            hidden_ch=ch+self.rcrhdn_size

        if self.thinker=='cat':
            node_in=2
        elif self.thinker=='add':
            node_in=1
        else:
            raise NotImplementedError('Unknown thinker option for NodeAttFunction: {}'.format(self.thinker))
        self.useGlobal=useGlobal
        if useGlobal:
            node_in+=1
        dropN=[]
        actM=[]
        self.actN=[]
        self.actN_u=[]
        actR=[]
        if 'group' in norm:
            if useGlobal:
                self.actN_u.append(nn.GroupNorm(getGroupSize(ch),ch))
            actM.append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
            if self.use_rcrhdn:
                actR.append(nn.GroupNorm(getGroupSize(ch),ch))
            if more_norm:
                dropN.append(nn.GroupNorm(getGroupSize(ch*node_in+rcrhdn_size),ch*node_in+rcrhdn_size))
                self.norm1N = nn.GroupNorm(getGroupSize(ch),ch)
                self.norm1E = nn.GroupNorm(getGroupSize(ch),ch)
            else:
                self.actN.append(nn.GroupNorm(getGroupSize(ch+rcrhdn_size),ch+rcrhdn_size_out))

        elif norm:
            raise NotImplemented('Norm: {}, not implmeneted for NodeAttFunc'.format(norm))
        if dropout is not None:
            if type(dropout) is float:
                da=dropout
            else:
                da=0.1
            actM.append(nn.Dropout(p=da,inplace=True))
            actR.append(nn.Dropout(p=da,inplace=True))
            dropN.append(nn.Dropout(p=da,inplace=True))
        actM.append(nn.ReLU(inplace=True))
        actR.append(nn.ReLU(inplace=True))
        if relu_node_act:
            self.actN.append(nn.ReLU(inplace=True))
        if not more_norm:
            self.actN=nn.Sequential(*self.actN)
        if useGlobal:
            self.actN_u=nn.Sequential(*self.actN_u)


        self.node_mlp = nn.Sequential(*dropN,nn.Linear(ch*node_in+rcrhdn_size, hidden_ch), *(actM), nn.Linear(hidden_ch, ch+rcrhdn_size_out))
        self.mhAtt = MultiHeadedAttention(heads,ch,mod=att_mod)
        if self.use_rcrhdn:
            #these layers are for providing initial values when cold-starting the recurrent net
            self.start_rcrhdn_nodes = nn.Sequential(*actR,nn.Linear(ch,self.rcrhdn_size))
            if self.use_rcrhdn=='gru':
                self.node_rcr = nn.GRU(input_size=ch*node_in,hidden_size=rcrhdn_size,num_layers=1)
                #maybe add normalization layer?

    def clear(self):
        self.rcrhdn_nodes=None

    def forward(self, x, edge_index, edge_attr, u, batch=None):
        if self.use_rcrhdn and self.rcrhdn_nodes is None:
            self.rcrhdn_nodes = self.start_rcrhdn_nodes(x)
        # x: [N, F_x], where N is the number of nodes.
        # edge_index: [2, E] with max entry N - 1.
        # edge_attr: [E, F_e]
        # u: [B, F_u]
        row, col = edge_index
        eRange = torch.arange(col.size(0))
        mask = torch.zeros(x.size(0), edge_attr.size(0))
        mask[col,eRange]=1
        mask = mask.to(x.device)

        if self.norm_before_att:
            x=self.norm1N(x)
            edge_attr=self.norm1E(edge_attr)

        #Add batch dimension
        x_b = x[None,...]
        edge_attr_b = edge_attr[None,...]
        g = self.mhAtt(x_b,edge_attr_b,edge_attr_b,mask) 
        
        #above uses unnormalized, unactivated features.
        g = g[0] #discard batch dim
        
        if not self.norm_before_att:
            if self.use_rcrhdn and self.use_rcrhdn!='gru':
                xa = self.actN(torch.cat((x,self.rcrhdn_nodes),dim=1))
            else:
                xa = self.actN(x)
        else:
            xa=x
        if u is not None:
            assert(u.size(0)==1)
            us = u.expand(source.size(0),u.size(1))
            us = self.actN_u(us)
            if self.thinker=='cat':
                input=torch.cat((xa,g,us),dim=1)
            elif self.thinker=='add':
                g+=xa
                input=torch.cat((g,us),dim=1)
        else:
            if self.thinker=='cat':
                input=torch.cat((xa,g),dim=1)
            elif self.thinker=='add':
                input= g+xa
        if self.norm_before_att:
            if self.use_rcrhdn and self.use_rcrhdn!='gru':
                input=torch.cat((input,self.rcrhdn_nodes),dim=1)
        if self.use_rcrhdn=='gru':
            self.rcrhdn_nodes = self.node_rcr(input[None,...], self.rcrhdn_nodes[None,...])[1][0]
            input = torch.cat((input,self.rcrhdn_nodes),dim=1)
        out= self.node_mlp(input)
        if self.use_rcrhdn  and self.use_rcrhdn!='gru':
            self.rcrhdn_nodes=out[:,-self.rcrhdn_size:]
            out=out[:,:-self.rcrhdn_size]
        if self.res:
            out+=x
        return out

class NoGlobalFunc(nn.Module):
    def __init__(self):
         super(NoGlobalFunc,self).__init__()
    def forward(self,x, edge_index, edge_attr, u, batch):
        return None
    def clear(self):
        pass

class GlobalAvgFunc(nn.Module):
    def __init__(self,ch,dropout=0.1,norm='group',useRes=True,hidden_ch=None,rcrhdn_size=0):
        super(GlobalAvgFunc, self).__init__()
        self.res=useRes

        actS=[]
        actM=[]
        actR=[]
        if 'group' in norm:
            actS.append(nn.GroupNorm(getGroupSize(3*ch+rcrhdn_size,24),3*ch+rcrhdn_size))
            actM.append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
            if self.use_rcrhdn:
                actR.append(nn.GroupNorm(getGroupSize(ch),ch))
        elif norm:
            raise NotImplemented('Norm: {}, not implmeneted for EdgeFunc'.format(norm))
        if dropout is not None:
            if type(dropout) is float or dropout==0:
                da=dropout
            else:
                da=0.1
            actS.append(nn.Dropout(p=da,inplace=True))
            actM.append(nn.Dropout(p=da,inplace=True))
            actR.append(nn.Dropout(p=da,inplace=True))
        actS.append(nn.ReLU(inplace=True))
        actM.append(nn.ReLU(inplace=True))
        actR.append(nn.ReLU(inplace=True))
        
        self.global_mlp = nn.Sequential(*(act[0]),nn.Linear(ch*3+rcrhdn_size, hidden_ch), *(act[1]), nn.Linear(hidden_ch, ch+rcrhdn_size_out))
        if self.use_rcrhdn:
            #these layers are for providing initial values when cold-starting the recurrent net
            self.start_rcrhdn_global = nn.Sequential(*act[8],nn.Linear(ch,self.rcrhdn_size))
            if self.use_rcrhdn=='gru':
                self.global_rcr = nn.GRU(input_size=ch*3,hidden_size=rcrhdn_size,num_layers=1)

    def clear(self):
        self.rcrhdn_global=None

    def forward(self,x, edge_index, edge_attr, u, batch):
        if self.use_rcrhdn and self.rcrhdn_global is None:
            self.rcrhdn_global = self.start_rcrhdn_global(u)
        # x: [N, F_x], where N is the number of nodes.
        # edge_index: [2, E] with max entry N - 1.
        # edge_attr: [E, F_e]
        # u: [B, F_u]
        # batch: [N] with max entry B - 1.
        if batch is None:
            out = torch.cat([u, torch.mean(x,dim=0),torch.mean(edge_attr,dim=0)],dim=1)
        else:
            raise NotImplemented('batching not implemented for scatter_mean of edge_attr')
            out = torch.cat([u, scatter_mean(x, batch, dim=0)], dim=1)
        if self.use_rcrhdn=='gru':
            raise NotImplemented('GRU not implemented for global, but is easy to do')
        if self.use_rcrhdn:
            out = torch.cat([out,self.rcrhdn_global],dim=1)
        out = self.global_mlp(out)
        if self.use_rcrhdn:
            self.rcrhdn_global=out[:,-self.rcrhdn_size:]
            out=out[:,:-self.rcrhdn_size]
        if self.res:
            out+=u
        return out

#This assumes the inputs are not activated
class MetaGraphLayer(nn.Module):
    def __init__(self,edge_m,node_m,global_m):
        super(MetaGraphLayer, self).__init__()
        self.op = MetaLayer(edge_m, node_m, global_m)

    def clear(self):
        self.op.edge_model.clear()
        self.op.node_model.clear()
        self.op.global_model.clear()

    def forward(self, input):
        x, edge_index, edge_attr, u = input
        batch=None
        x, edge_attr, u = self.op(x, edge_index, edge_attr=edge_attr, u=u, batch=batch)
        return x, edge_index, edge_attr, u



#1/(1+(e^(-(x+1)/0.5)))
class SharpSigmoid(nn.Module):
    def __init__(self,center,sharp=0.5):
        super(SharpSigmoid, self).__init__()
        self.c=center
        self.sharp=sharp
    def forward(self,input):
        return 1/(1+torch.exp(-(input+self.c)/self.sharp))


#This assumes the inputs are not activated
#class MetaGraphAttentionLayerOLD(nn.Module):
#    def __init__(self, ch,heads=4,dropout=0.1,norm='group',useRes=True,useGlobal=False,hidden_ch=None,agg_thinker='cat',soft_prune_edges=False,edge_decider=None,rcrhdn_size=0,relu_node_act=False,att_mod=False,avgEdges=False): 
#        super(MetaGraphAttentionLayerOLD, self).__init__()
#        
#        self.thinker=agg_thinker
#        self.soft_prune_edges=soft_prune_edges
#        self.res=useRes
#        self.avgEdges=avgEdges
#
#        if rcrhdn_size!=0:
#            if rcrhdn_size<0:
#                rcrhdn_size*=-1
#                rcrhdn_size_out=0
#                self.use_rcrhdn='gru'
#            else:
#                #use a special memory channel for recurrent applications of this layer
#                self.use_rcrhdn = True
#                rcrhdn_size_out=rcrhdn_size
#            self.rcrhdn_size = rcrhdn_size
#            self.rcrhdn_edges=None
#            self.rcrhdn_nodes=None
#            if useGlobal:
#                self.rcrhdn_global=None
#        else:
#            self.rcrhdn_size = 0
#            rcrhdn_size_out = 0
#            self.use_rcrhdn = False
#        if hidden_ch is None:
#            hidden_ch=ch+self.rcrhdn_size
#
#        edge_in=3
#        if self.thinker=='cat':
#            node_in=2
#        else:
#            node_in=1
#        act=[[] for i in range(9)]
#        dropN=[]
#        self.actN=[]
#        if useGlobal:
#            self.actN_u=[]
#        if 'group' in norm:
#            #for i in range(5):
#                #act[i].append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
#            #global    
#            act[0].append(nn.GroupNorm(getGroupSize(3*ch+rcrhdn_size,24),3*ch+rcrhdn_size))
#            act[1].append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
#            #edge
#            act[2].append(nn.GroupNorm(getGroupSize(edge_in*ch+rcrhdn_size,edge_in*8),edge_in*ch+rcrhdn_size))
#            act[3].append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
#            #node
#            self.actN.append(nn.GroupNorm(getGroupSize(ch+rcrhdn_size),ch+rcrhdn_size_out))
#            if useGlobal:
#                self.actN_u.append(nn.GroupNorm(getGroupSize(ch),ch))
#            act[4].append(nn.GroupNorm(getGroupSize(hidden_ch),hidden_ch))
#            #decider
#            act[5].append(nn.GroupNorm(getGroupSize(ch),ch))
#
#            if self.use_rcrhdn:
#                for i in range(6,9):
#                    act[i].append(nn.GroupNorm(getGroupSize(ch),ch))
#        elif 'batch' in norm:
#            raise NotImplemented('Havent added rcrhdn_size')
#            #for i in range(5):
#            #    act[i].append(nn.BatchNorm1d(ch))
#            #global
#            act[0].append(nn.BatchNorm1d(3*ch))
#            act[1].append(nn.BatchNorm1d(hidden_ch))
#            #edge
#            act[2].append(nn.BatchNorm1d(edge_in*ch))
#            act[3].append(nn.BatchNorm1d(hidden_ch))
#            #node
#            self.actN.append(nn.BatchNorm1d(ch))
#            if useGlobal:
#                self.actN_u.append(nn.BatchNorm1d(ch))
#            act[4].append(nn.BatchNorm1d(hidden_ch))
#            #decider
#            act[5].append(nn.BatchNorm1d(ch))
#        elif 'instance' in norm:
#            raise NotImplemented('Havent added rcrhdn_size')
#            #for i in range(5):
#            #    act[i].append(nn.InstanceNorm1d(ch))
#            #global
#            act[0].append(nn.InstanceNorm1d(3*ch))
#            act[1].append(nn.InstanceNorm1d(hidden_ch))
#            #edge
#            act[2].append(nn.InstanceNorm1d(edge_in*ch))
#            act[3].append(nn.InstanceNorm1d(hidden_ch))
#            #node
#            self.actN.append(nn.InstanceNorm1d(ch))
#            if useGlobal:
#                self.actN_u.append(nn.InstanceNorm1d(ch))
#            act[4].append(nn.InstanceNorm1d(hidden_ch))
#            #decider
#            act[5].append(nn.InstanceNorm1d(ch))
#        else:
#            raise NotImplemented('Unknown norm: {}'.format(norm))
#        if dropout is not None:
#            if type(dropout) is float:
#                da=dropout
#            else:
#                da=0.1
#            for i in range(len(act)):
#                act[i].append(nn.Dropout(p=da,inplace=True))
#            dropN.append(nn.Dropout(p=da,inplace=True))
#        for i in range(len(act)):
#            act[i].append(nn.ReLU(inplace=True))
#        if relu_node_act:
#            self.actN.append(nn.ReLU(inplace=True))
#        self.actN=nn.Sequential(*self.actN)
#        if useGlobal:
#            self.actN_u=nn.Sequential(*self.actN_u)
#
#        self.useGlobal=useGlobal
#        if useGlobal:
#            edge_in +=1
#            node_in +=1
#            self.global_mlp = nn.Sequential(*(act[0]),nn.Linear(ch*3+rcrhdn_size, hidden_ch), *(act[1]), nn.Linear(hidden_ch, ch+rcrhdn_size_out))
#
#        self.edge_mlp = nn.Sequential(*(act[2]),nn.Linear(ch*edge_in+rcrhdn_size, hidden_ch), *(act[3]), nn.Linear(hidden_ch, ch+rcrhdn_size_out))
#        
#        if self.soft_prune_edges:
#            if edge_decider is None:
#                self.edge_decider = nn.Sequential(*(act[5]),nn.Linear(ch, 1), nn.Sigmoid())
#                # we shift the mean up to bias keeping edges (should help begining of training
#                self.edge_decider[len(act[5])].bias = nn.Parameter(self.edge_decider[len(act[5])].bias.data + 2.0/self.edge_decider[len(act[5])].bias.size(0))
#            else:
#                # we shouldn't need that bias here since it's already getting trained
#                self.edge_decider = nn.Sequential(edge_decider, SharpSigmoid(-1))
#        else:
#            self.edge_decider = None
#        self.node_mlp = nn.Sequential(*dropN,nn.Linear(ch*node_in+rcrhdn_size, hidden_ch), *(act[4]), nn.Linear(hidden_ch, ch+rcrhdn_size_out))
#        self.mhAtt = MultiHeadedAttention(heads,ch,mod=att_mod)
#
#        if self.use_rcrhdn:
#            #these layers are for providing initial values when cold-starting the recurrent net
#            self.start_rcrhdn_nodes = nn.Sequential(*act[6],nn.Linear(ch,self.rcrhdn_size))
#            self.start_rcrhdn_edges = nn.Sequential(*act[7],nn.Linear(ch,self.rcrhdn_size))
#            if self.useGlobal:
#                self.start_rcrhdn_edges = nn.Sequential(*act[8],nn.Linear(ch,self.rcrhdn_size))
#            if self.use_rcrhdn=='gru':
#                self.edge_rcr = nn.GRU(input_size=ch*edge_in,hidden_size=rcrhdn_size,num_layers=1)
#                self.node_rcr = nn.GRU(input_size=ch*node_in,hidden_size=rcrhdn_size,num_layers=1)
#
#
#        def edge_model(source, target, edge_attr, u):
#            # source, target: [E, F_x], where E is the number of edges.
#            # edge_attr: [E, F_e]
#            # u: [B, F_u], where B is the number of graphs.
#            if u is not None:
#                assert(u.size(0)==1)
#                us = u.expand(source.size(0),u.size(1))
#                out = torch.cat([source, target, edge_attr,us], dim=1)
#            else:
#                out = torch.cat([source, target, edge_attr], dim=1)
#            if self.use_rcrhdn=='gru':
#                self.rcrhdn_edges = self.edge_rcr(out[None,...], self.rcrhdn_edges[None,...])[1][0]
#            if self.use_rcrhdn:
#                out = torch.cat([out,self.rcrhdn_edges],dim=1)
#            out = self.edge_mlp(out)
#            if self.use_rcrhdn and self.use_rcrhdn!='gru':
#                self.rcrhdn_edges=out[:,-self.rcrhdn_size:]
#                out=out[:,:-self.rcrhdn_size]
#                
#
#            if self.soft_prune_edges:
#                pruneDecision = self.edge_decider(out)
#                #print(pruneDecision)
#                out *= self.soft_prune_edges
#            if self.res:
#                out+=edge_attr
#            if self.avgEdges: #assumes bidirection edges repeated in order
#                avg = (out[:out.size(0)//2] + out[out.size(0)//2:])/2
#                out = avg.repeat(2,1)
#
#            return out
#
#        def node_model(x, edge_index, edge_attr, u):
#            # x: [N, F_x], where N is the number of nodes.
#            # edge_index: [2, E] with max entry N - 1.
#            # edge_attr: [E, F_e]
#            # u: [B, F_u]
#            row, col = edge_index
#            eRange = torch.arange(col.size(0))
#            mask = torch.zeros(x.size(0), edge_attr.size(0))
#            mask[col,eRange]=1
#            mask = mask.to(x.device)
#            #Add batch dimension
#            x_b = x[None,...]
#            edge_attr_b = edge_attr[None,...]
#            g = self.mhAtt(x_b,edge_attr_b,edge_attr_b,mask) 
#            #above uses unnormalized, unactivated features.
#            g = g[0] #discard batch dim
#
#            if self.use_rcrhdn and self.use_rcrhdn!='gru':
#                xa = self.actN(torch.cat((x,self.rcrhdn_nodes),dim=1))
#            else:
#                xa = self.actN(x)
#            if u is not None:
#                assert(u.size(0)==1)
#                us = u.expand(source.size(0),u.size(1))
#                us = self.actN_u(us)
#                if self.thinker=='cat':
#                    input=torch.cat((xa,g,us),dim=1)
#                elif self.thinker=='add':
#                    g+=xa
#                    input=torch.cat((g,us),dim=1)
#            else:
#                if self.thinker=='cat':
#                    input=torch.cat((xa,g),dim=1)
#                elif self.thinker=='add':
#                    input= g+xa
#            if self.use_rcrhdn=='gru':
#                self.rcrhdn_nodes = self.node_rcr(input[None,...], self.rcrhdn_nodes[None,...])[1][0]
#                input = torch.cat((input,self.rcrhdn_nodes),dim=1)
#            out= self.node_mlp(input)
#            if self.use_rcrhdn  and self.use_rcrhdn!='gru':
#                self.rcrhdn_nodes=out[:,-self.rcrhdn_size:]
#                out=out[:,:-self.rcrhdn_size]
#            if self.res:
#                out+=x
#            return out
#
#        def global_model(x, edge_index, edge_attr, u, batch):
#            # x: [N, F_x], where N is the number of nodes.
#            # edge_index: [2, E] with max entry N - 1.
#            # edge_attr: [E, F_e]
#            # u: [B, F_u]
#            # batch: [N] with max entry B - 1.
#            if self.useGlobal:
#                if batch is None:
#                    out = torch.cat([u, torch.mean(x,dim=0),torch.mean(edge_attr,dim=0)],dim=1)
#                else:
#                    raise NotImplemented('batching not implemented for scatter_mean of edge_attr')
#                    out = torch.cat([u, scatter_mean(x, batch, dim=0)], dim=1)
#                if self.use_rcrhdn=='gru':
#                    raise NotImplemented('GRU not implemented for global, but is easy to do')
#                if self.use_rcrhdn:
#                    out = torch.cat([out,self.rcrhdn_global],dim=1)
#                out = self.global_mlp(out)
#                if self.use_rcrhdn:
#                    self.rcrhdn_global=out[:,-self.rcrhdn_size:]
#                    out=out[:,:-self.rcrhdn_size]
#                if self.res:
#                    out+=u
#                return out
#            else:
#                return None
#
#        self.layer = MetaLayer(edge_model, node_model, global_model)
#
#    def forward(self, input): 
#        node_features, edge_indexes, edge_features, u_features = input
#        if self.use_rcrhdn and self.rcrhdn_nodes is None:
#            self.rcrhdn_nodes = self.start_rcrhdn_nodes(node_features)
#            self.rcrhdn_edges = self.start_rcrhdn_edges(edge_features)
#            if self.useGlobal:
#                self.rcrhdn_global = self.start_rcrhdn_global(u_features)
#        node_features,edge_features,u_features = self.layer(node_features, edge_indexes, edge_features, u_features)
#        return node_features, edge_indexes, edge_features, u_features
#
#    def clear(self):
#        self.rcrhdn_nodes = None
#        self.rcrhdn_edges = None
#        self.rcrhdn_global = None

class MetaGraphNet(nn.Module):
    def __init__(self, config): # predCount, base_0, base_1):
        super(MetaGraphNet, self).__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.validate_input = config['debug_check_input'] if 'debug_check_input' in config else False

        
        self.useRepRes = config['use_repetition_res'] if 'use_repetition_res' in config else False
        #how many times to re-apply main layers
        self.repetitions = config['repetitions'] if 'repetitions' in config else 1 
        self.randomReps = False

        self.undirected = (not config['directed']) if 'directed' in config else True

        ch = config['in_channels']
        layerType = config['layer_type'] if 'layer_type' in config else 'attention'
        layerCount = config['num_layers'] if 'num_layers' in config else 3
        numNodeOut = config['node_out']
        numEdgeOut = config['edge_out']
        norm = config['norm'] if 'norm' in config else 'group'
        dropout = config['dropout'] if 'dropout' in config else 0.1
        hasEdgeInfo = config['input_edge'] if 'input_edge' in config else True

        if 'better_norm_attention' in config and config['better_norm_attention']:
            node_att_thinker='add'
            node_att_more_norm=True
        else:
            node_att_thinker='cat'
            node_att_more_norm=False

        if 'node_att_thinker' in config:
            node_att_thinker = config['node_att_thinker']

        edge_sep_norm = 'better_norm_edge' in config and config['better_norm_edge']


        self.trackAtt=False

        actN=[]
        actE=[]
        if 'group' in norm:
            actN.append(nn.GroupNorm(getGroupSize(ch),ch))
            actE.append(nn.GroupNorm(getGroupSize(ch),ch))
        elif norm:
            raise NotImplemented('Havent implemented other norms ({}) in MetaGraphNet'.format(norm))
        if dropout:
            actN.append(nn.Dropout(p=dropout,inplace=True))
            actE.append(nn.Dropout(p=dropout,inplace=True))
        actN.append(nn.ReLU(inplace=True))
        actE.append(nn.ReLU(inplace=True))
        if numNodeOut>0:
            self.node_out_layers=nn.Sequential(*actN,nn.Linear(ch,numNodeOut))
        else:
            self.node_out_layers=lambda x:  None
        if numEdgeOut>0:
            self.edge_out_layers=nn.Sequential(*actE,nn.Linear(ch,numEdgeOut))
        else:
            self.edge_out_layers=lambda x:  None

        useGlobal=None

        rcrhdn_size = config['rcrhdn_size'] if 'rcrhdn_size' in config else 0
        avgEdges = config['avg_edges'] if 'avg_edges' in config else False
        soft_prune_edges = config['soft_prune_edges'] if 'soft_prune_edges' in config else False
        if 'prune_with_classifier' in config and config['prune_with_classifier']:
            edge_decider = self.edge_out_layers
        else:
            edge_decider = None
        if soft_prune_edges=='last':
            soft_prune_edges_l = ([False]*(layerCount-1)) + [True]
        elif type(soft_prune_edges) is int:
            soft_prune_edges_l = ([True]*soft_prune_edges) + ([False]*(layerCount-1-soft_prune_edges))
        elif soft_prune_edges:
            soft_prune_edges_l = [True]*layerCount
        else:
            soft_prune_edges_l = [False]*layerCount
        soft_prune_edges_l.append(False)
        rcrhdn_size = [rcrhdn_size]*layerCount
        rcrhdn_size.append(0)

        if layerType=='attention':
            att_mod = config['att_mod'] if 'att_mod' in config else False
            relu_node_act = config['relu_node_act'] if 'relu_node_act' in config else 0
            heads = config['num_heads'] if 'num_heads' in config else 4

            def getEdgeFunc(i):
                return EdgeFunc(ch,dropout=dropout,norm=norm,useRes=True,useGlobal=useGlobal,hidden_ch=None,soft_prune_edges=soft_prune_edges_l[i],edge_decider=edge_decider,rcrhdn_size=rcrhdn_size[i],avgEdges=avgEdges,sep_norm=edge_sep_norm)
            def getNodeFunc(i):
                return NodeAttFunc(ch,heads=heads,dropout=dropout,norm=norm,useRes=True,useGlobal=useGlobal,hidden_ch=None,agg_thinker=node_att_thinker,rcrhdn_size=rcrhdn_size[i],att_mod=att_mod,more_norm=node_att_more_norm)
            def getGlobalFunc(i):
                if useGlobal:
                    return GlobalFunc(ch,heads=heads,dropout=dropout,norm=norm,useRes=True,hidden_ch=None,rcrhdn_size=rcrhdn_size[i])
                else:
                    return NoGlobalFunc()


            #layers = [MetaGraphAttentionLayerOLD(ch,heads=heads,dropout=dropout,norm=norm,useRes=True,useGlobal=False,hidden_ch=None,agg_thinker='cat',soft_prune_edges=soft_prune_edges_l[i],edge_decider=edge_decider,rcrhdn_size=rcrhdn_size[i],relu_node_act=relu_node_act,att_mod=att_mod,avgEdges=avgEdges) for i in range(layerCount)]
        elif layerType=='tree':
            def getEdgeFunc(i):
                return EdgeFunc(ch,dropout=dropout,norm=norm,useRes=True,useGlobal=useGlobal,hidden_ch=None,soft_prune_edges=soft_prune_edges_l[i],edge_decider=edge_decider,rcrhdn_size=rcrhdn_size[i],avgEdges=avgEdges)
            def getNodeFunc(i):
                return NodeTreeFunc(ch,dropout=dropout,norm=norm,useRes=True,useGlobal=useGlobal,hidden_ch=None,rcrhdn_size=rcrhdn_size[i])
            def getGlobalFunc(i):
                if useGlobal:
                    return GlobalFunc(ch,heads=heads,dropout=dropout,norm=norm,useRes=True,hidden_ch=None,rcrhdn_size=rcrhdn_size[i])
                else:
                    return NoGlobalFunc()
        elif layerType=='mean':
            def getEdgeFunc(i):
                return EdgeFunc(ch,dropout=dropout,norm=norm,useRes=True,useGlobal=useGlobal,hidden_ch=None,soft_prune_edges=soft_prune_edges_l[i],edge_decider=edge_decider,rcrhdn_size=rcrhdn_size,avgEdges=avgEdges)
            def getNodeFunc(i):
                return NodeMeanFunc(ch,heads=heads,dropout=dropout,norm=norm,useRes=True,useGlobal=useGlobal,hidden_ch=None,agg_thinker='cat',rcrhdn_size=rcrhdn_size,att_mod=att_mod)
            def getGlobalFunc(i):
                if useGlobal:
                    return GlobalFunc(ch,heads=heads,dropout=dropout,norm=norm,useRes=True,hidden_ch=None,rcrhdn_size=rcrhdn_size)
                else:
                    return NoGlobalFunc()
            layers = [MetaGraphMeanLayer(ch,False) for i in range(layerCount)]
            self.main_layers = nn.Sequential(*layers)
        else:
            print('Unknown layer type: {}'.format(layerType))
            exit()
        layers = [MetaGraphLayer(getEdgeFunc(i),getNodeFunc(i),getGlobalFunc(i)) for i in range(layerCount)]
        self.main_layers = nn.Sequential(*layers)

        self.input_layers=None
        if 'encode_type' in config:
            inputLayerType = config['encode_type']
            if 'fc' in inputLayerType:
                infeats = config['infeats']
                infeatsEdge = config['infeats_edge'] if 'infeats_edge' in config else 0
                self.input_layers = MetaGraphFCEncoderLayer(infeats,infeatsEdge,ch)
            if inputLayerType!='fc':
                #layer = MetaGraphAttentionLayerOLD(ch,heads=heads,dropout=dropout,norm=norm,useRes=True,useGlobal=False,hidden_ch=None,agg_thinker='cat',relu_node_act=relu_node_act,att_mod=att_mod,avgEdges=avgEdges)
                layer = MetaGraphLayer(getEdgeFunc(-1),getNodeFunc(-1),getGlobalFunc(-1))
                if self.input_layers is None:
                    self.input_layers = layer
                else:
                    self.input_layers = nn.Sequential(self.input_layers,layer)
            self.force_encoding = config['force_encoding'] if 'force_encoding' in config else False
        else:
            assert(hasEdgeInfo==True)




    def forward(self, input):
        node_features, edge_indexes, edge_features, u_features = input
        if self.validate_input:
            assert(node_features.min()<0 and 'Im assuming the input has not been ReLUed')
            assert(node_features.max()<900)
            assert(edge_features.size(0)==0 or edge_features.max()<900)
        if self.randomReps:
            if self.training:
                repetitions=np.random.randint(self.minReps,self.maxReps+1)
            else:
                repetitions=self.maxReps
        else:
            repetitions=self.repetitions

        #if self.useRes:
            #node_featuresA = self.act_layers(node_features)
            #edge_featuresA = self.act_layers(edge_features)
            #if u_features is not None:
            #    u_featuresA = self.act_layers(u_features)
            #else:
            #    u_featuresA = None

        out_nodes = []
        out_edges = [] #for holding each repititions outputs, so we can backprop on all of them

        if self.trackAtt:
            self.attn=[]

        if self.input_layers is not None:
            node_features, edge_indexes, edge_features, u_features = self.input_layers((node_features, edge_indexes, edge_features, u_features))
            if self.trackAtt:
                self.attn.append(self.input_layers.mhAtt.attn)

            if self.force_encoding:
                node_out = self.node_out_layers(node_features)
                edge_out = self.edge_out_layers(edge_features)

                if node_out is not None:
                    out_nodes.append(node_out)
                if edge_out is not None:
                    out_edges.append(edge_out)
        
        for i in range(repetitions):
            node_featuresT, edge_indexesT, edge_featuresT, u_featuresT = self.main_layers((node_features, edge_indexes, edge_features, u_features))
            if self.trackAtt:
                for layer in self.main_layers:
                    self.attn.append(layer.mhAtt.attn)
            if self.useRepRes:
                node_features=node_features+node_featuresT
                edge_features=edge_features+edge_featuresT
                if u_features is not None:
                    u_features=u_features+u_featuresT
                else:
                    u_features=u_featuresT
            else:
                node_features=node_featuresT
                edge_features=edge_featuresT
                u_features=u_featuresT
            
            node_out = self.node_out_layers(node_features)
            edge_out = self.edge_out_layers(edge_features)

            if node_out is not None:
                out_nodes.append(node_out)
            if edge_out is not None:
                out_edges.append(edge_out)
        for layer in self.main_layers:
            layer.clear()

        if len(out_nodes)>0:
            out_nodes = torch.stack(out_nodes,dim=1) #we introduce a 'time' dimension
        else:
            out_nodes = None
        if len(out_edges)>0:
            out_edges = torch.stack(out_edges,dim=1) #we introduce a 'time' dimension, indexXtimeXclass
        else:
            out_edges = None

        if self.undirected:
            out_edges = (out_edges[:out_edges.size(0)//2] + out_edges[out_edges.size(0)//2:])/2
            edge_features = (edge_features[:edge_features.size(0)//2] + edge_features[edge_features.size(0)//2:])/2
        if self.validate_input:
            assert(node_features.max()<1000)
            assert(edge_features.max()<1000)

        return out_nodes, out_edges, node_features, edge_features, u_features



    def summary(self):
        """
        Model summary
        """
        model_parameters = filter(lambda p: p.requires_grad, self.parameters())
        params = sum([np.prod(p.size()) for p in model_parameters])
        self.logger.info('Trainable parameters: {}'.format(params))
        self.logger.info(self)
