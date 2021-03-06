python train_model.py \
 --logs_base_dir   /home/chenzhijian/code/tf_face/logs \
 --models_base_dir /home/chenzhijian/code/tf_face/models  \
 --model_def       models.inception_resnet_v1_6c \
 --lfw_file_ext    _face_.jpg \
 --lfw_pairs       /exports_data/czj/data/lfw/files/pairs.txt  \
 --lfw_dir         /exports_data/czj/data/lfw/lfw_aligned/ \
 --lfw_batch_size  100 \
 --data_dir        /exports_data/czj/data/casia/casia_aligned/ \
 --imglist         /exports_data/czj/data/casia/files/train_set.txt \
 --image_size      160 \
 --optimizer       RMSPROP \
 --learning_rate   -1 \
 --learning_rate_schedule_file lr_decay.txt \
 --batch_size      90 \
 --epoch_size      1000 \
 --max_num_epochs  80 \
 --keep_prob       0.8 \
 --random_flip \
 --random_crop \
 --weight_decay    5e-5 \
 --center_loss_factor 0.0001 \
 --center_loss_alpha 0.9 \
 --gpu_id 1
