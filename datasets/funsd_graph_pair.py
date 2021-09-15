import torch.utils.data
import numpy as np
import json
#from skimage import io
#from skimage import draw
#import skimage.transform as sktransform
import os
import math, random
from collections import defaultdict, OrderedDict
from utils.funsd_annotations import createLines
import timeit
from .graph_pair import GraphPairDataset

import utils.img_f as img_f


def collate(batch):
    assert(len(batch)==1) #only batchsize of 1 allowed
    return batch[0]


class FUNSDGraphPair(GraphPairDataset):
    """
    Class for reading forms dataset and creating starting and ending gt
    """


    def __init__(self, dirPath=None, split=None, config=None, images=None):
        super(FUNSDGraphPair, self).__init__(dirPath,split,config,images)

        self.only_types=None

        self.split_to_lines = config['split_to_lines']

        if images is not None:
            self.images=images
        else:
            if 'overfit' in config and config['overfit']:
                splitFile = 'overfit_split.json'
            else:
                splitFile = 'FUNSD_train_valid_test_split.json'
            with open(os.path.join(splitFile)) as f:
                readFile = json.loads(f.read())
                if type(split) is str:
                    toUse = readFile[split]
                    imagesAndAnn = []
                    imageDir = os.path.join(dirPath,toUse['root'],'images')
                    annDir = os.path.join(dirPath,toUse['root'],'annotations')
                    for name in toUse['images']:
                        imagesAndAnn.append( (name+'.png',os.path.join(imageDir,name+'.png'),os.path.join(annDir,name+'.json')) )
                elif type(split) is list:
                    imagesAndAnn = []
                    for spstr in split:
                        toUse = readFile[spstr]
                        imageDir = os.path.join(dirPath,toUse['root'],'images')
                        annDir = os.path.join(dirPath,toUse['root'],'annotations')
                        for name in toUse['images']:
                            imagesAndAnn.append( (name+'.png',os.path.join(imageDir,name+'.png'),os.path.join(annDir,name+'.json')) )
                else:
                    print("Error, unknown split {}".format(split))
                    exit()
            self.images=[]
            for imageName,imagePath,jsonPath in imagesAndAnn:
                org_path = imagePath
                if self.cache_resized:
                    path = os.path.join(self.cache_path,imageName)
                else:
                    path = org_path
                if os.path.exists(jsonPath):
                    rescale=1.0
                    if self.cache_resized:
                        rescale = self.rescale_range[1]
                        if not os.path.exists(path):
                            org_img = img_f.imread(org_path)
                            if org_img is None:
                                print('WARNING, could not read {}'.format(org_img))
                                continue
                            resized = img_f.resize(org_img,(0,0),
                                    fx=self.rescale_range[1], 
                                    fy=self.rescale_range[1], 
                                    )
                            img_f.imwrite(path,resized)
                    self.images.append({'id':imageName, 'imagePath':path, 'annotationPath':jsonPath, 'rescaled':rescale, 'imageName':imageName[:imageName.rfind('.')]})
        self.only_types=None
        self.errors=[]

        self.classMap={
                'header':16,
                'question':17,
                'answer': 18,
                'other': 19
                }
        self.index_class_map=[
                'header',
                'question',
                'answer',
                'other'
                ]





    def parseAnn(self,annotations,s):

        numClasses=len(self.classMap)
        if self.split_to_lines:
            bbs, numNeighbors, trans, groups = createLines(annotations,self.classMap,s)
        else:
            boxes = annotations['form']
            bbs = np.empty((1,len(boxes), 8+8+numClasses), dtype=np.float32) #2x4 corners, 2x4 cross-points, n classes
            #pairs=set()
            numNeighbors=[]
            trans=[]
            for j,boxinfo in enumerate(boxes):
                lX,tY,rX,bY = boxinfo['box']
                h=bY-tY
                w=rX-lX
                if h/w>5 and self.rotate: #flip labeling, since FUNSD doesn't label verticle text correctly
                    #I don't know if it needs rotated clockwise or countercw, so I just say countercw
                    bbs[:,j,0]=lX*s
                    bbs[:,j,1]=bY*s
                    bbs[:,j,2]=lX*s
                    bbs[:,j,3]=tY*s
                    bbs[:,j,4]=rX*s
                    bbs[:,j,5]=tY*s
                    bbs[:,j,6]=rX*s
                    bbs[:,j,7]=bY*s
                    #we add these for conveince to crop BBs within window
                    bbs[:,j,8]=s*(lX+rX)/2.0
                    bbs[:,j,9]=s*bY
                    bbs[:,j,10]=s*(lX+rX)/2.0
                    bbs[:,j,11]=s*tY
                    bbs[:,j,12]=s*lX
                    bbs[:,j,13]=s*(tY+bY)/2.0
                    bbs[:,j,14]=s*rX
                    bbs[:,j,15]=s*(tY+bY)/2.0
                else:
                    bbs[:,j,0]=lX*s
                    bbs[:,j,1]=tY*s
                    bbs[:,j,2]=rX*s
                    bbs[:,j,3]=tY*s
                    bbs[:,j,4]=rX*s
                    bbs[:,j,5]=bY*s
                    bbs[:,j,6]=lX*s
                    bbs[:,j,7]=bY*s
                    #we add these for conveince to crop BBs within window
                    bbs[:,j,8]=s*lX
                    bbs[:,j,9]=s*(tY+bY)/2.0
                    bbs[:,j,10]=s*rX
                    bbs[:,j,11]=s*(tY+bY)/2.0
                    bbs[:,j,12]=s*(lX+rX)/2.0
                    bbs[:,j,13]=s*tY
                    bbs[:,j,14]=s*(rX+lX)/2.0
                    bbs[:,j,15]=s*bY
                
                bbs[:,j,16:]=0
                if boxinfo['label']=='header':
                    bbs[:,j,16]=1
                elif boxinfo['label']=='question':
                    bbs[:,j,17]=1
                elif boxinfo['label']=='answer':
                    bbs[:,j,18]=1
                elif boxinfo['label']=='other':
                    bbs[:,j,19]=1

                trans.append(boxinfo['text'])
                numNeighbors.append(len(boxinfo['linking']))
            groups = [[n] for n in range(len(boxes))]

        word_boxes=[]
        word_trans=[]
        for entity in annotations['form']:
            for word in entity['words']:
                lX,tY,rX,bY = word['box']
                h = bY-tY +1
                w = rX-lX +1
                bb=[None]*16
                if h/w>5 and self.rotate: #flip labeling, since FUNSD doesn't label verticle text correctly
                    #I don't know if it needs rotated clockwise or countercw, so I just say countercw
                    bb[0]=lX*s
                    bb[1]=bY*s
                    bb[2]=lX*s
                    bb[3]=tY*s
                    bb[4]=rX*s
                    bb[5]=tY*s
                    bb[6]=rX*s
                    bb[7]=bY*s
                    #w these for conveince to crop BBs within window
                    bb[8]=s*(lX+rX)/2.0
                    bb[9]=s*bY
                    bb[10]=s*(lX+rX)/2.0
                    bb[11]=s*tY
                    bb[12]=s*lX
                    bb[13]=s*(tY+bY)/2.0
                    bb[14]=s*rX
                    bb[15]=s*(tY+bY)/2.0
                else:
                    bb[0]=lX*s
                    bb[1]=tY*s
                    bb[2]=rX*s
                    bb[3]=tY*s
                    bb[4]=rX*s
                    bb[5]=bY*s
                    bb[6]=lX*s
                    bb[7]=bY*s
                    #w these for conveince to crop BBs within window
                    bb[8]=s*lX
                    bb[9]=s*(tY+bY)/2.0
                    bb[10]=s*rX
                    bb[11]=s*(tY+bY)/2.0
                    bb[12]=s*(lX+rX)/2.0
                    bb[13]=s*tY
                    bb[14]=s*(rX+lX)/2.0
                    bb[15]=s*bY
                word_boxes.append(bb)
                word_trans.append(word['text'])
        word_boxes = np.array(word_boxes)
        return bbs, list(range(bbs.shape[1])), numClasses, trans, groups, {}, {'word_boxes':word_boxes, 'word_trans':word_trans}


    def getResponseBBIdList(self,queryId,annotations):
        if self.split_to_lines:
            return annotations['linking'][queryId]
        else:
            boxes=annotations['form']
            cto=[]
            boxinfo = boxes[queryId]
            for id1,id2 in boxinfo['linking']:
                if id1==queryId:
                    cto.append(id2)
                else:
                    cto.append(id1)
            return cto



def getWidthFromBB(bb):
    return (np.linalg.norm(bb[0]-bb[1]) + np.linalg.norm(bb[3]-bb[2]))/2
def getHeightFromBB(bb):
    return (np.linalg.norm(bb[0]-bb[3]) + np.linalg.norm(bb[1]-bb[2]))/2



