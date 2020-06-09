import numpy as np
import os
import torch
import torchvision

from scipy.io import loadmat
from PIL import Image
from pycocotools.coco import COCO
from torch.utils.data import Dataset

from segmentation.augment import \
    MultiRandomAffineCrop, MultiCenterAffineCrop, ImageAugmentor
from utils.params import ParamDict as o

class SegEncoder:

    def __init__(self, num_classes):
        """__init__.

        Args:
            num_classes:    number of classes
        """
        self.num_classes = num_classes

    def catgory_to_onehot(self, cat_map_pil):
        cat_map_1hw = self.pil_to_tensor(cat_map_pil)
        cat_ids_n = torch.arange(1, self.num_classes+1, dtype=cat_map_1hw.dtype)
        cat_ids_n11 = cat_ids_n[:, None, None]
        return (cat_ids_n11 == cat_map_1hw).float()

    def pil_to_tensor(self, pil_img):
        img = torch.ByteTensor(torch.ByteStorage.from_buffer(pil_img.tobytes()))
        img = img.view(pil_img.size[1], pil_img.size[0], len(pil_img.getbands()))
        img = img.permute((2, 0, 1)).contiguous()
        return img

    def __call__(self, data_dict):
        tmp_dict = data_dict.copy()

        return {
            'image_b3hw': tmp_dict['image'],
            'seg_mask_bnhw': self.catgory_to_onehot(tmp_dict['seg_mask']),
            'loss_mask_bnhw': self.pil_to_tensor(tmp_dict['loss_mask']).float(),
        }

class BaseSet(Dataset):
    '''
    Abstract base set

    To use this dataset implementation, simply inherent this class and
    implement the following methods:
        - __init__: overwrite init to implement necessary initialization.
            But don't forget to invoke parent constructor as well!
        - get_raw_data(self, key): return a dictionary comprising of data
            at dataset[key].
        - __len__: return the size of the underlying dataset.
    '''
    DEFAULT_PARAMS = o(
        crop_params=MultiRandomAffineCrop.DEFAULT_PARAMS,
        augment_params=ImageAugmentor.DEFAULT_PARAMS,
        color_jitter=o(
            brightness=0.3,
            contrast=0.3,
            saturation=0.3,
            hue=0.1,
        )
    )

    def __init__(self, params, train):
        self.p = params
        self.mode = 'train' if train else 'val'
        if train:
            self.multi_crop = MultiRandomAffineCrop(self.p.crop_params)
            self.img_augmentor = ImageAugmentor(self.p.augment_params)
        else:
            self.multi_crop = MultiCenterAffineCrop(self.p.crop_params)
            self.img_augmentor = torchvision.transforms.ToTensor()
        self.encoder = SegEncoder(len(self.p.classes))
        self.color_jitter = torchvision.transforms.ColorJitter(
            brightness=self.p.color_jitter.brightness,
            contrast=self.p.color_jitter.contrast,
            saturation=self.p.color_jitter.saturation,
            hue = self.p.color_jitter.hue
        )

    def get_raw_data(self, key):
        raise NotImplementedError("Pure Virtual Method")

    def __getitem__(self, key):
        raw_data = self.get_raw_data(key)
        crop_data = self.multi_crop(raw_data)
        crop_data['image'] = self.img_augmentor(crop_data['image'])
        enc_data = self.encoder(crop_data)
        return enc_data

class COCODataset(BaseSet):
    DEFAULT_PARAMS = BaseSet.DEFAULT_PARAMS(
        version=2017,
        data_dir='/data/COCO2017',
        annotation_dir='/data/COCO2017/annotations',
        min_area=200,
        classes=set([
            'bottle',
            'wine glass',
            'cup',
            'fork',
            'knife',
            'spoon',
            'bowl',
            'chair',
            'couch',
            'bed',
            'dining table',
            'toilet',
            'laptop',
            'mouse',
            'remote',
            'keyboard',
            'microwave',
            'oven',
            'toaster',
            'sink',
            'refrigerator',
        ]),
    )

    def __init__(self, params=DEFAULT_PARAMS, train=True):
        super(COCODataset, self).__init__(params, train)
        self.annotation_path = os.path.join(self.p.annotation_dir,
            'instances_{}{}.json'.format(self.mode, self.p.version))
        self.img_dir = os.path.join(self.p.data_dir,
            '{}{}'.format(self.mode, self.p.version))
        self.coco = COCO(self.annotation_path)
        self.img_ids = list(self.coco.imgs.keys())
        self.class_map = self._generate_class_map()

    def _generate_class_map(self):
        idx = 1
        mapping = {}
        for cat_id, cat in self.coco.cats.items():
            if cat['name'] in self.p.classes:
                mapping[cat_id] = idx
                idx += 1
        return mapping

    def _get_img(self, img_id):
        img_desc = self.coco.imgs[img_id]
        img_fname = img_desc['file_name']
        img_fpath = os.path.join(self.img_dir, img_fname)
        return Image.open(img_fpath).convert('RGB')

    def get_raw_data(self, key):
        assert isinstance(key, int), "non integer key not supported!"
        img_id = self.img_ids[key]
        annotations = self.coco.imgToAnns[img_id]
        img = self._get_img(img_id)
        seg_mask = torch.zeros((img.size[1], img.size[0]), dtype=torch.uint8)
        loss_mask = torch.ones_like(seg_mask)
        for ann in annotations:
            ann_mask = torch.from_numpy(self.coco.annToMask(ann))
            # mask indicating invalid regions
            if ann['iscrowd'] or ann['area'] < self.p.min_area:
                loss_mask = torch.bitwise_and(loss_mask, torch.bitwise_not(ann_mask))
            elif ann['category_id'] in self.class_map:
                class_id = self.class_map[ann['category_id']]
                seg_mask = torch.max(seg_mask, ann_mask*class_id)
        return {
            'image': self._get_img(img_id),
            'seg_mask': seg_mask,
            'loss_mask': loss_mask,
        }

    def __len__(self):
        return len(self.coco.imgs)

class ADE20KDataset(BaseSet):
    '''
    Fine-grained instance-level segmentation data from the 2016 ADE20K challenge.

    Data can be grabbed from http://data.csail.mit.edu/places/ADEchallenge/ADEChallengeData2016.zip
    '''
    DEFAULT_PARAMS = BaseSet.DEFAULT_PARAMS(
        root_dir = "/data/ADEChallengeData2016/",
        classes=set([
            "wall",
            "floor, flooring",
            "ceiling",
            "bed",
            "cabinet",
            "door, double door",
            "table",
            "curtain, drape, drapery, mantle, pall",
            "chair",
            "sofa, couch, lounge",
            "shelf",
            "armchair",
            "seat",
            "desk",
            "lamp",
            "chest of drawers, chest, bureau, dresser",
            "pillow",
            "screen door, screen",
            "coffee table, cocktail table",
            "toilet, can, commode, crapper, pot, potty, stool, throne",
            "kitchen island",
            "computer, computing machine, computing device, data processor, electronic computer, information processing system",
            "swivel chair",
            "pole",
            "bannister, banister, balustrade, balusters, handrail",
            "cradle",
            "oven",
            "screen, silver screen, projection screen",
            "blanket, cover",
            "tray",
            "crt screen",
            "plate",
            "monitor, monitoring device"
        ])
    )

    def __init__(self, params=DEFAULT_PARAMS, train=True):
        '''
        Initialize and load the ADE20K annotation file into memory.
        '''
        super(ADE20KDataset, self).__init__(params, train)
        root_dir = self.p.root_dir
        if train:
            img_dir = os.path.join(root_dir, "images/training")
            seg_anno_path = os.path.join(root_dir, "annotations/training")
        else:
            img_dir = os.path.join(root_dir, "images/validation")
            seg_anno_path = os.path.join(root_dir, "annotations/validation")
        anno_path = os.path.join(root_dir, "sceneCategories.txt")
        class_desc_path = os.path.join(root_dir, "objectInfo150.txt")
        # Load file paths and annotations
        with open(anno_path) as f:
            anno_content = f.readlines()

        self.img_path_list = []
        self.scenario_list = []
        self.seg_path_list = []

        for line in anno_content:
            img_name, scene_name = line[:-1].split(' ') # remove eol
            if train and "val" in img_name:
                continue
            if not train and "train" in img_name:
                continue
            img_path = os.path.join(img_dir, img_name + '.jpg')
            seg_path = os.path.join(seg_anno_path, img_name + '.png')
            self.img_path_list.append(img_path)
            self.seg_path_list.append(seg_path)
            self.scenario_list.append(scene_name)

        self.dataset_size = len(self.img_path_list)
        self.class_map = self._generate_class_map(class_desc_path)

    def get_raw_data(self, key):
        """
        Args:
            key (int): key

        Returns:
            ret_dict
        """
        assert isinstance(key, int), "non integer key not supported!"
        img_path = self.img_path_list[key]
        seg_path = self.seg_path_list[key]
        img = Image.open(img_path).convert('RGB')
        seg_mask = np.array(Image.open(seg_path), dtype = np.uint8)
        seg_mask = self.class_map(seg_mask)
        seg_mask = torch.tensor(seg_mask, dtype = torch.uint8)
        loss_mask = torch.ones_like(seg_mask)
        return {'image': img, 'seg_mask': seg_mask, 'loss_mask': loss_mask}

    def _generate_class_map(self, class_desc_path):
        # Take subset of class
        with open(class_desc_path) as f:
            class_desc = f.readlines()

        class_desc = class_desc[1:] # Remove header
        class_desc = [line[:-1].split('\t') for line in class_desc] # remove eol
        class_name_list = [line[-1].strip(' ') for line in class_desc]

        map_dict = {}
        cur_idx = 1 # Background maps to 0
        for i in range(len(class_name_list)):
            n = class_name_list[i]
            if n in self.p.classes:
                # Original class id is i + 1.
                map_dict[i + 1] = cur_idx
                cur_idx += 1
        # Factory map function
        def map_func(elem):
            if elem in map_dict:
                return map_dict[elem]
            else:
                return 0 # Map everything else to zero
        vectorized_map_func = np.vectorize(map_func)
        return vectorized_map_func

    def __len__(self):
        return self.dataset_size

class FineGrainedADE20KDataset(BaseSet):
    '''
    Fine-grained instance-level segmentation data from the 2016 ADE20K challenge.

    Data can be grabbed from https://groups.csail.mit.edu/vision/datasets/ADE20K/ADE20K_2016_07_26.zip
    '''
    DEFAULT_PARAMS = BaseSet.DEFAULT_PARAMS(
        root_dir = "/data/ADE20K_2016_07_26/",
        classes=set([
            'bottle',
            'wine glass',
            'cup',
            'fork',
            'knife',
            'spoon',
            'bowl',
            'chair',
            'couch',
            'bed',
            'dining table',
            'toilet',
            'laptop',
            'mouse',
            'remote',
            'keyboard',
            'microwave',
            'oven',
            'toaster',
            'sink',
            'refrigerator',
        ])
    )

    def __init__(self, params=DEFAULT_PARAMS, train=True):
        '''
        Initialize and load the ADE20K annotation file into memory.
        '''
        super(FineGrainedADE20KDataset, self).__init__(params, train)
        self.ds = loadmat(os.path.join(self.p.root_dir, "index_ade20k.mat"))
        self.ds = self.ds['index']
        self.data_dir_base = os.path.join(self.p.root_dir, '..')
        img_set_mark = 'train' if train else 'val'
        self.img_path_list = []
        self.seg_path_list = []
        for i in range(self.ds['filename'][0, 0].shape[1]):
            cur_file_name = self.ds['filename'][0, 0][0, i][0]
            if img_set_mark in cur_file_name:
                folder_path = self.ds['folder'][0, 0][0, i][0]
                img_path = os.path.join(self.data_dir_base, folder_path, cur_file_name)
                seg_path = FineGrainedADE20KDataset.get_seg_path(img_path)
                self.img_path_list.append(img_path)
                self.seg_path_list.append(seg_path)
        self.dataset_size = len(self.img_path_list)

    def get_raw_data(self, key):
        """
        Args:
            key (int): key

        Returns:
            ret_dict
        """
        assert isinstance(key, int), "non integer key not supported!"
        img_path = self.img_path_list[key]
        seg_path = self.seg_path_list[key]
        raw_img = np.array(Image.open(img_path).convert('RGB'), dtype = np.uint8)
        seg_img = np.array(Image.open(seg_path), dtype = np.uint8)
        cat_map = seg_img[:,:,0] // 10
        cat_map = cat_map.astype(np.int)
        cat_map = cat_map * 256
        cat_map = cat_map + seg_img[:,:,1]
        seg_mask = cat_map
        # seg_mask = self.class_map(seg_mask)
        seg_mask = torch.tensor(seg_mask, dtype = torch.int64)
        loss_mask = torch.ones_like(seg_mask)
        return {'image': raw_img, 'seg_mask': seg_mask, 'loss_mask': loss_mask}

    def _generate_class_map(self, class_desc_path):
        # Take subset of class
        with open(class_desc_path) as f:
            class_desc = f.readlines()

        class_desc = class_desc[1:] # Remove header
        class_desc = [line[:-1].split('\t') for line in class_desc] # remove eol
        class_name_list = [line[-1].strip(' ') for line in class_desc]

        map_dict = {}
        cur_idx = 1 # Background maps to 0
        for i in range(len(class_name_list)):
            n = class_name_list[i]
            if n in self.p.classes:
                # Original class id is i + 1.
                map_dict[i + 1] = cur_idx
                cur_idx += 1
        # Factory map function
        def map_func(elem):
            if elem in map_dict:
                return map_dict[elem]
            else:
                return 0 # Map everything else to zero
        vectorized_map_func = np.vectorize(map_func)
        return vectorized_map_func

    def __len__(self):
        return self.dataset_size
    
    @staticmethod
    def get_seg_path(img_path):
        return img_path[:-4] + '_seg.png'

if __name__ == '__main__':
    import matplotlib.pyplot as plt
    ds = FineGrainedADE20KDataset(train = True)
    print(ds.ds['objectnames'][0, 0][0, 975])
    print("Size: {}".format(len(ds)))
    data_dict = ds.get_raw_data(0)
    print(data_dict['seg_mask'])
    plt.imshow(data_dict['seg_mask'] == 976)
    plt.show()