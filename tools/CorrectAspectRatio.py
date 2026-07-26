import cv2

# Open input video
cap = cv2.VideoCapture('2028x1520x8bit.h264')

# Define the correct target width and height
target_width = 5404
target_height = 1520  # e.g., mapping to a 32:9 ratio

# Setup video writer
fourcc = cv2.VideoWriter_fourcc(*'h264')
fps = cap.get(cv2.CAP_PROP_FPS)
out = cv2.VideoWriter('corrected_video.h264', fourcc, fps, (target_width, target_height))

while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break
    
    # Resize the frame to fix the aspect ratio distortion
    resized_frame = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_LINEAR)
    
    out.write(resized_frame)

cap.release()
out.release()
cv2.destroyAllWindows()




# jeremyhubbard@SoccerCam:~ $ rpicam-hello --list-camera
# Available cameras
# -----------------
# 0 : imx477 [4056x3040 12-bit RGGB] (/base/soc/i2c0mux/i2c@1/imx477@1a)
#     Modes: 'SRGGB10_CSI2P' : 1332x990 [120.50 fps - (696, 528)/2664x1980 crop]
#                              2028x1080 [74.74 fps - (0, 440)/4056x2160 crop]
#                              2028x1520 [53.77 fps - (0, 0)/4056x3040 crop]
#                              4056x2160 [19.58 fps - (0, 440)/4056x2160 crop]
#                              4056x3040 [14.00 fps - (0, 0)/4056x3040 crop]
#            'SRGGB12_CSI2P' : 1332x990 [101.68 fps - (696, 528)/2664x1980 crop]
#                              2028x1080 [62.81 fps - (0, 440)/4056x2160 crop]
#                              2028x1520 [45.19 fps - (0, 0)/4056x3040 crop]
#                              4056x2160 [16.39 fps - (0, 440)/4056x2160 crop]
#                              4056x3040 [11.72 fps - (0, 0)/4056x3040 crop]
#            'SRGGB8' : 1332x990 [147.91 fps - (696, 528)/2664x1980 crop]
#                       2028x1080 [92.27 fps - (0, 440)/4056x2160 crop]
#                       2028x1520 [66.38 fps - (0, 0)/4056x3040 crop]
#                       4056x2160 [24.32 fps - (0, 440)/4056x2160 crop]
#                       4056x3040 [17.39 fps - (0, 0)/4056x3040 crop]
